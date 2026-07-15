"""
billing.py — Pro plan catalogue and entitlement granting.

Scope: this module owns *what a plan costs* and *how a plan gets granted*. It
deliberately does not implement a payment provider — see "Wiring up payments".

Wiring up payments
------------------
An entitlement may only be granted by something that proves a payment happened
for a known user. That means a hosted checkout plus a signed webhook:

  1. Create a payment link/page per plan with a provider that supports
     recurring billing (Razorpay and Stripe both do, and both support UPI for
     Indian customers). Put the links in BILLING_CHECKOUT_URL_MONTHLY /
     _YEARLY, and the webhook signing secret in BILLING_WEBHOOK_SECRET.
  2. Pass the user id through checkout as client reference / notes metadata so
     the webhook can identify who paid.
  3. Implement `verify_webhook()` below with the provider's signature check,
     then call `grant_pro()`.

A bare UPI/GPay deep link (upi://pay?pa=...) cannot close this loop. It sends
no callback, carries no user id, and is invisible to this server — so nothing
could unlock the account even after the money arrives. It also only resolves on
a mobile device with a UPI app installed, which is not where this app is used.
Recurring collection additionally needs UPI Autopay via a registered merchant.
"""

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
    # notes[*] is Razorpay's metadata convention on Payment Pages, and the only
    # thing echoed back on the webhook entity — so the user id and plan must
    # travel there or the grant can't be attributed. extract_grant() reads both
    # of these back out.
    params = urlencode({
        "notes[client_reference_id]": user_id,
        "notes[plan_id]": plan_id,
    })
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{params}"


def verify_webhook(raw_body: bytes, signature: str) -> bool:
    """Constant-time signature check over the raw request body.

    Both Razorpay (X-Razorpay-Signature) and Stripe (Stripe-Signature) sign the
    exact bytes received, so callers must pass the raw body — not a re-encoded
    dict, whose key order and separators would differ and break the digest.
    """
    secret = settings.billing_webhook_secret
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def grant_seconds(plan_id: str) -> int:
    return _GRANT_SECONDS.get(plan_id, _GRANT_SECONDS[MONTHLY])


# Razorpay events that mean "money arrived, grant the plan". subscription.charged
# covers renewals, so a yearly/monthly mandate re-grants on each cycle.
GRANTING_EVENTS = {"payment.captured", "subscription.charged", "order.paid"}


def is_granting_event(event: dict) -> bool:
    """False for the many webhook types that must not unlock anything —
    payment.failed, subscription.cancelled, refunds, and so on.

    Razorpay sends every subscribed event to the same endpoint, so the event
    name has to gate the grant; a valid signature only proves Razorpay sent it,
    not that it was a successful payment.
    """
    name = event.get("event")
    # No event name = the flat test/manual shape; the caller's field checks
    # are the gate there.
    return name in GRANTING_EVENTS if name else True


def extract_grant(event: dict) -> tuple[str | None, str | None]:
    """Pull (user_id, plan_id) out of a verified webhook payload.

    Handles Razorpay's nested shape (notes ride on the payment/subscription
    entity) and falls back to a flat {client_reference_id, plan_id} body.

    `notes` is where Razorpay echoes back the metadata a checkout was created
    with — it's the only field that can carry our user id through the payment
    round-trip, so checkout_url() must put it there.
    """
    entity = {}
    payload = event.get("payload")
    if isinstance(payload, dict):
        for key in ("subscription", "payment", "order"):
            section = payload.get(key)
            if isinstance(section, dict) and isinstance(section.get("entity"), dict):
                entity = section["entity"]
                break

    notes = entity.get("notes") if isinstance(entity.get("notes"), dict) else {}

    user_id = (
        notes.get("client_reference_id")
        or notes.get("user_id")
        or event.get("client_reference_id")
    )
    plan_id = notes.get("plan_id") or event.get("plan_id")
    if plan_id not in (MONTHLY, YEARLY):
        plan_id = None
    return user_id, plan_id
