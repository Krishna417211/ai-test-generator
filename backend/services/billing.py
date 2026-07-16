"""billing.py — Pro plan catalogue and Stripe entitlement granting.

Scope: this module owns *what a plan costs* and *how a webhook is read*. It holds
no Stripe SDK and no secret key — the checkout side is a hosted Stripe Payment
Link, and the grant side is a signed webhook. Everything here is pure functions
over payloads; the store writes live in main.billing_webhook.

Wiring up payments
------------------
An entitlement may only be granted by something that proves a payment happened
for a known user:

  1. Create a Stripe Payment Link per plan (monthly, yearly) and put them in
     BILLING_CHECKOUT_URL_MONTHLY / _YEARLY.
  2. Add a webhook endpoint pointing at /api/billing/webhook, subscribed to
     checkout.session.completed and invoice.paid. Put its signing secret
     (whsec_...) in BILLING_WEBHOOK_SECRET.
  3. Nothing else. checkout_url() tags the link so the webhook can identify the
     payer, and main.billing_webhook does the granting.

Why client_reference_id carries both ids
----------------------------------------
A Payment Link accepts exactly one piece of caller state in its query string:
`?client_reference_id=...`, echoed back on the Checkout Session. There is no
arbitrary-metadata query parameter, so the plan has to ride inside that single
field alongside the user id — hence the `usr_x__monthly` encoding below. Stripe
restricts the value to alphanumerics, dashes and underscores, which is why the
separator is `__` and not `:`.

Why renewals need the customer mapping
--------------------------------------
Only the Checkout Session echoes client_reference_id. The invoice.paid that
arrives on every subsequent renewal identifies the payer solely by Stripe
customer id, so the first payment has to record customer -> user (see
store.link_stripe_customer) or every subscriber would lapse after one period.
"""

import re
import time
import hmac
import logging
import hashlib
from urllib.parse import urlencode

from config import settings

logger = logging.getLogger(__name__)

MONTHLY = "monthly"
YEARLY = "yearly"

# A Pro grant lasts this long past payment; the webhook re-grants on each
# renewal, and get_plan() lets a missed renewal lapse back to free on its own.
_GRANT_SECONDS = {MONTHLY: 31 * 24 * 3600, YEARLY: 366 * 24 * 3600}

# Stripe's recurring interval -> our plan id. A renewal invoice names the
# interval it billed, which is how a renewal recovers the plan without us
# having stored it.
_INTERVAL_TO_PLAN = {"month": MONTHLY, "year": YEARLY}

# Stripe rejects a client_reference_id outside this charset, and silently drops
# it rather than failing the checkout — so a bad one costs a grant.
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_REFERENCE_SEP = "__"

# How far in the past a signature timestamp may be before it reads as a replay.
# Matches Stripe's own default tolerance.
WEBHOOK_TOLERANCE_SECONDS = 300


class BillingNotConfigured(Exception):
    """No payment provider is set up on this server yet."""


def plans() -> list[dict]:
    """The plan catalogue the pricing UI renders."""
    monthly = settings.pro_price_monthly_usd
    yearly = settings.pro_price_yearly_usd
    return [
        {
            "id": MONTHLY,
            "name": "Pro Monthly",
            "price_usd": monthly,
            "interval": "month",
            "checkout_url": settings.billing_checkout_url_monthly or None,
        },
        {
            "id": YEARLY,
            "name": "Pro Yearly",
            "price_usd": yearly,
            "interval": "year",
            "checkout_url": settings.billing_checkout_url_yearly or None,
            # e.g. 20*12 - 100 = 140
            "savings_usd": max(0, monthly * 12 - yearly),
        },
    ]


def make_client_reference(user_id: str, plan_id: str) -> str:
    """Pack (user, plan) into the one field a Payment Link round-trips."""
    ref = f"{user_id}{_REFERENCE_SEP}{plan_id}"
    if not _REFERENCE_RE.match(ref):
        # Refuse rather than hand out a link Stripe would strip the tag from:
        # the payment would succeed and the grant would be unattributable.
        raise BillingNotConfigured(
            f"user id {user_id!r} cannot be encoded into a Stripe client_reference_id"
        )
    return ref


def parse_client_reference(ref: str) -> tuple[str | None, str | None]:
    """Unpack what make_client_reference packed. Unknown plans read as None."""
    if not ref or _REFERENCE_SEP not in ref:
        return None, None
    # Partition from the right: user ids carry an underscore of their own
    # ("usr_abc"), so only the last separator is the one we wrote.
    user_id, _, plan_id = ref.rpartition(_REFERENCE_SEP)
    return (user_id or None), (plan_id if plan_id in (MONTHLY, YEARLY) else None)


def checkout_url(plan_id: str, user_id: str) -> str:
    """Hosted checkout link for a plan, tagged so the webhook can identify the payer.

    Raises BillingNotConfigured when no link is set, so the UI can show a real
    "not available yet" state instead of a button that goes nowhere.
    """
    urls = {
        MONTHLY: settings.billing_checkout_url_monthly,
        YEARLY: settings.billing_checkout_url_yearly,
    }
    url = urls.get(plan_id)
    if not url:
        raise BillingNotConfigured(
            f"No checkout link is configured for the {plan_id} plan."
        )
    params = urlencode({"client_reference_id": make_client_reference(user_id, plan_id)})
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{params}"


def _parse_signature_header(header: str) -> tuple[int | None, list[str]]:
    """Split Stripe-Signature ("t=123,v1=abc,v1=def") into its timestamp and
    candidate signatures. More than one v1 appears while a signing secret is
    being rotated, and any of them may be the valid one.
    """
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return None, []
        elif key == "v1":
            signatures.append(value)
    return timestamp, signatures


def verify_webhook(raw_body: bytes, signature_header: str, now: float | None = None) -> bool:
    """Constant-time check of Stripe's Stripe-Signature header.

    Stripe does not sign the body on its own: it signs "{timestamp}.{body}", and
    ships the timestamp in the same header. Callers must pass the raw bytes —
    a re-encoded dict would differ in key order and separators and never match.

    The timestamp is also what makes a captured webhook un-replayable, so it is
    checked, not just read.
    """
    secret = settings.billing_webhook_secret
    if not secret or not signature_header:
        return False

    timestamp, signatures = _parse_signature_header(signature_header)
    if timestamp is None or not signatures:
        return False

    now = time.time() if now is None else now
    # Only lateness is rejected. A future timestamp means clock skew between us
    # and Stripe, and can't be forged without the secret anyway.
    if now - timestamp > WEBHOOK_TOLERANCE_SECONDS:
        logger.warning("Rejected a billing webhook older than the replay tolerance")
        return False

    signed_payload = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


def grant_seconds(plan_id: str) -> int:
    return _GRANT_SECONDS.get(plan_id, _GRANT_SECONDS[MONTHLY])


# The two Stripe events that mean "money arrived, grant the plan".
# checkout.session.completed covers the first payment and carries the payer tag;
# invoice.paid covers every renewal after it.
GRANTING_EVENTS = {"checkout.session.completed", "invoice.paid"}

# A Checkout Session can complete without the money having actually landed —
# delayed methods finish the session as "unpaid" and settle later (or fail).
_PAID_STATUSES = {"paid", "no_payment_required"}


def event_object(event: dict) -> dict:
    """The entity a Stripe event is about (data.object)."""
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    return obj if isinstance(obj, dict) else {}


def is_granting_event(event: dict) -> bool:
    """False for the many webhook types that must not unlock anything —
    invoice.payment_failed, customer.subscription.deleted, refunds, and so on.

    Stripe sends every subscribed event to the same endpoint, so the type has to
    gate the grant; a valid signature only proves Stripe sent it, not that it
    was a successful payment.
    """
    event_type = event.get("type")
    if event_type not in GRANTING_EVENTS:
        return False
    if event_type == "checkout.session.completed":
        # Deny by default: an unrecognised payment_status is not a payment.
        return event_object(event).get("payment_status") in _PAID_STATUSES
    if event_type == "invoice.paid":
        # A new subscription fires this *and* checkout.session.completed, in no
        # guaranteed order — and only the session carries the payer tag. Letting
        # the session own the first grant removes the race: if this invoice won
        # it, no customer mapping exists yet and it could not be attributed
        # anyway. Renewals (subscription_cycle) are this event's actual job.
        # A payload with no billing_reason at all still grants: that shape is not
        # a known first invoice, and refusing it would drop a real renewal.
        return event_object(event).get("billing_reason") != "subscription_create"
    return True


def extract_customer(event: dict) -> str | None:
    """The Stripe customer id on a session or invoice, expanded or not."""
    customer = event_object(event).get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    return customer if isinstance(customer, str) and customer else None


def _plan_from_invoice(obj: dict) -> str | None:
    """Recover the plan from a renewal invoice by the interval it billed."""
    lines = obj.get("lines")
    entries = lines.get("data") if isinstance(lines, dict) else None
    for line in entries or []:
        if not isinstance(line, dict):
            continue
        # price.recurring is the current shape; plan is the legacy one, still
        # sent on older API versions.
        for source in (line.get("price"), line.get("plan")):
            if not isinstance(source, dict):
                continue
            recurring = source.get("recurring")
            interval = (
                recurring.get("interval") if isinstance(recurring, dict)
                else source.get("interval")
            )
            if interval in _INTERVAL_TO_PLAN:
                return _INTERVAL_TO_PLAN[interval]
    return None


def extract_grant(event: dict) -> tuple[str | None, str | None]:
    """Pull (user_id, plan_id) out of a verified webhook payload.

    A checkout session carries both, in the client_reference_id we tagged its
    link with. A renewal invoice carries neither: its plan comes from the billed
    interval, and its user_id is None because only the customer id identifies
    the payer — the caller resolves that through store.get_user_by_stripe_customer.
    """
    obj = event_object(event)
    event_type = event.get("type")

    if event_type == "invoice.paid":
        return None, _plan_from_invoice(obj)

    user_id, plan_id = parse_client_reference(obj.get("client_reference_id") or "")
    if user_id and plan_id:
        return user_id, plan_id

    # Sessions created through the API (rather than a Payment Link) can carry
    # real metadata, which survives without the packing dance.
    metadata = obj.get("metadata")
    if isinstance(metadata, dict):
        user_id = user_id or metadata.get("client_reference_id") or metadata.get("user_id")
        meta_plan = metadata.get("plan_id")
        plan_id = plan_id or (meta_plan if meta_plan in (MONTHLY, YEARLY) else None)

    return user_id or None, plan_id or None
