"""test_billing.py — Plan catalogue, Stripe checkout links, and webhook grants.

The bar here is the same as test_admin.py: every negative test guards money or a
privilege. A bug that grants Pro to nobody is a support ticket; one that grants
it to the wrong account, or to anyone who can POST an unsigned body, is not.
"""

import time
import hmac
import hashlib
import tempfile

import pytest
from fastapi.testclient import TestClient

from services import billing
from services.store import JobStore

SECRET = "whsec_test"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(billing.settings, "billing_checkout_url_monthly", "https://pay.example/m")
    monkeypatch.setattr(billing.settings, "billing_checkout_url_yearly", "https://pay.example/y?ref=1")
    monkeypatch.setattr(billing.settings, "billing_webhook_secret", SECRET)


def _sign(body: bytes, secret: str = SECRET, t: int | None = None) -> str:
    """Build a Stripe-Signature header the way Stripe builds it: the digest is
    over "{timestamp}.{body}", not the body alone."""
    t = int(time.time()) if t is None else t
    digest = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def _session(ref="usr_9__monthly", customer="cus_1", payment_status="paid", **extra) -> dict:
    obj = {
        "id": "cs_1",
        "object": "checkout.session",
        "payment_status": payment_status,
        "customer": customer,
    }
    if ref is not None:
        obj["client_reference_id"] = ref
    obj.update(extra)
    return {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": obj}}


def _invoice(customer="cus_1", interval="month", billing_reason="subscription_cycle") -> dict:
    return {
        "id": "evt_2",
        "type": "invoice.paid",
        "data": {"object": {
            "object": "invoice",
            "customer": customer,
            "billing_reason": billing_reason,
            "lines": {"data": [{"price": {"recurring": {"interval": interval}}}]},
        }},
    }


class TestPlans:
    def test_catalogue_prices(self):
        by_id = {p["id"]: p for p in billing.plans()}
        assert by_id["monthly"]["price_usd"] == 20
        assert by_id["yearly"]["price_usd"] == 100

    def test_yearly_shows_the_saving_against_12_months(self):
        yearly = next(p for p in billing.plans() if p["id"] == "yearly")
        assert yearly["savings_usd"] == 140  # 20*12 - 100

    def test_checkout_url_is_none_until_configured(self, monkeypatch):
        monkeypatch.setattr(billing.settings, "billing_checkout_url_monthly", "")
        assert billing.plans()[0]["checkout_url"] is None


class TestCheckoutUrl:
    def test_tags_the_url_with_user_and_plan(self, configured):
        assert "client_reference_id=usr_9__monthly" in billing.checkout_url("monthly", "usr_9")

    def test_appends_to_an_existing_query_string(self, configured):
        assert billing.checkout_url("yearly", "usr_9").startswith("https://pay.example/y?ref=1&")

    def test_raises_when_no_provider_is_set_up(self, monkeypatch):
        monkeypatch.setattr(billing.settings, "billing_checkout_url_monthly", "")
        with pytest.raises(billing.BillingNotConfigured):
            billing.checkout_url("monthly", "usr_9")

    def test_round_trips_through_extract_grant(self, configured):
        """The link we hand out must carry what the webhook needs to read back."""
        from urllib.parse import urlparse, parse_qs
        url = billing.checkout_url("yearly", "usr_42")
        ref = parse_qs(urlparse(url).query)["client_reference_id"][0]
        assert billing.extract_grant(_session(ref=ref)) == ("usr_42", "yearly")


class TestClientReference:
    def test_refuses_a_user_id_stripe_would_mangle(self):
        """Stripe silently drops a client_reference_id outside its charset, so the
        payment would land with nothing to attribute it to. Fail at link time."""
        with pytest.raises(billing.BillingNotConfigured):
            billing.make_client_reference("usr:9", "monthly")

    def test_keeps_the_underscore_inside_a_user_id(self):
        assert billing.parse_client_reference("usr_ab_cd__yearly") == ("usr_ab_cd", "yearly")

    def test_unknown_plan_is_rejected(self):
        assert billing.parse_client_reference("usr_9__lifetime") == ("usr_9", None)

    def test_untagged_reference_yields_nothing(self):
        assert billing.parse_client_reference("usr_9") == (None, None)
        assert billing.parse_client_reference("") == (None, None)


class TestWebhookVerification:
    def test_accepts_a_correct_signature(self, configured):
        body = b'{"type":"invoice.paid"}'
        assert billing.verify_webhook(body, _sign(body)) is True

    def test_rejects_a_body_signed_the_razorpay_way(self, configured):
        """A bare HMAC of the body — no timestamp, no v1= — is not a Stripe
        signature, and must not be accepted just because the secret matches."""
        body = b'{"type":"invoice.paid"}'
        bare = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert billing.verify_webhook(body, bare) is False

    def test_rejects_a_tampered_body(self, configured):
        assert billing.verify_webhook(b'{"a":2}', _sign(b'{"a":1}')) is False

    def test_rejects_a_wrong_secret(self, configured):
        body = b"{}"
        assert billing.verify_webhook(body, _sign(body, "whsec_guess")) is False

    def test_rejects_a_missing_signature(self, configured):
        assert billing.verify_webhook(b"{}", "") is False

    def test_rejects_a_malformed_header(self, configured):
        assert billing.verify_webhook(b"{}", "t=notanumber,v1=abc") is False
        assert billing.verify_webhook(b"{}", "v1=abc") is False

    def test_rejects_everything_when_no_secret_is_set(self, monkeypatch):
        """An unconfigured server must not accept unsigned grant requests."""
        monkeypatch.setattr(billing.settings, "billing_webhook_secret", "")
        body = b"{}"
        assert billing.verify_webhook(body, _sign(body, "")) is False

    def test_rejects_a_stale_signature(self, configured):
        """The timestamp is what makes a captured webhook un-replayable."""
        body = b"{}"
        stale = int(time.time()) - billing.WEBHOOK_TOLERANCE_SECONDS - 60
        assert billing.verify_webhook(body, _sign(body, t=stale)) is False

    def test_accepts_a_signature_inside_the_tolerance(self, configured):
        body = b"{}"
        recent = int(time.time()) - billing.WEBHOOK_TOLERANCE_SECONDS + 30
        assert billing.verify_webhook(body, _sign(body, t=recent)) is True

    def test_accepts_any_of_several_signatures(self, configured):
        """Stripe sends one v1 per active secret while a secret is rotating."""
        body = b"{}"
        t = int(time.time())
        good = _sign(body, t=t).split("v1=")[1]
        assert billing.verify_webhook(body, f"t={t},v1=deadbeef,v1={good}") is True


class TestGrantingEvents:
    def test_a_paid_session_grants(self):
        assert billing.is_granting_event(_session()) is True

    def test_a_session_with_no_money_yet_does_not_grant(self):
        """Delayed payment methods complete the session before the money lands —
        and may still fail. payment_status, not the event name, is the proof."""
        assert billing.is_granting_event(_session(payment_status="unpaid")) is False

    def test_an_unknown_payment_status_does_not_grant(self):
        assert billing.is_granting_event(_session(payment_status="weird")) is False

    def test_a_free_trial_session_grants(self):
        assert billing.is_granting_event(_session(payment_status="no_payment_required")) is True

    def test_a_renewal_invoice_grants(self):
        assert billing.is_granting_event(_invoice()) is True

    def test_the_first_invoice_of_a_subscription_does_not_grant(self):
        """It duplicates checkout.session.completed, arrives in no fixed order,
        and carries no payer tag — the session owns the first grant."""
        assert billing.is_granting_event(_invoice(billing_reason="subscription_create")) is False

    def test_an_invoice_without_a_billing_reason_still_grants(self):
        event = _invoice()
        del event["data"]["object"]["billing_reason"]
        assert billing.is_granting_event(event) is True

    @pytest.mark.parametrize("name", [
        "invoice.payment_failed", "customer.subscription.deleted",
        "charge.refunded", "payment_intent.created", "checkout.session.expired",
    ])
    def test_everything_else_does_not_grant(self, name):
        """A failed payment is signed just like a paid one — only the type
        separates them, so this is what stops a free upgrade."""
        assert billing.is_granting_event({"type": name, "data": {"object": {}}}) is False


class TestExtractGrant:
    def test_reads_a_session_reference(self):
        assert billing.extract_grant(_session()) == ("usr_9", "monthly")

    def test_reads_api_session_metadata(self):
        """Sessions made through the API (not a Payment Link) can carry real
        metadata instead of the packed reference."""
        event = _session(ref=None, metadata={"client_reference_id": "usr_3", "plan_id": "yearly"})
        assert billing.extract_grant(event) == ("usr_3", "yearly")

    def test_an_invoice_names_a_plan_but_no_user(self):
        """Only the customer id identifies a renewal's payer; the caller resolves it."""
        assert billing.extract_grant(_invoice(interval="year")) == (None, "yearly")

    def test_reads_the_legacy_invoice_plan_shape(self):
        event = _invoice()
        event["data"]["object"]["lines"]["data"] = [{"plan": {"interval": "year"}}]
        assert billing.extract_grant(event) == (None, "yearly")

    def test_an_unknown_interval_yields_no_plan(self):
        assert billing.extract_grant(_invoice(interval="week")) == (None, None)

    def test_an_untagged_session_yields_nothing(self):
        assert billing.extract_grant(_session(ref=None)) == (None, None)

    def test_missing_fields_yield_none(self):
        assert billing.extract_grant({}) == (None, None)


class TestExtractCustomer:
    def test_reads_a_customer_id(self):
        assert billing.extract_customer(_session()) == "cus_1"

    def test_reads_an_expanded_customer_object(self):
        assert billing.extract_customer(_session(customer={"id": "cus_2"})) == "cus_2"

    def test_no_customer_yields_none(self):
        assert billing.extract_customer(_session(customer=None)) is None


class TestGrantSeconds:
    def test_yearly_outlasts_monthly(self):
        assert billing.grant_seconds("yearly") > billing.grant_seconds("monthly")

    def test_monthly_covers_a_long_month(self):
        assert billing.grant_seconds("monthly") >= 31 * 24 * 3600


@pytest.fixture
def webhook(monkeypatch):
    """The webhook route over a throwaway store.

    main.py binds the store singleton by name at import, so the patch has to
    target main's own reference to it.
    """
    import main
    store = JobStore(tempfile.mktemp(suffix=".db"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(billing.settings, "billing_webhook_secret", SECRET)

    client = TestClient(main.app)

    def post(event: dict, *, signature: str | None = None):
        import json
        body = json.dumps(event).encode()
        return client.post(
            "/api/billing/webhook",
            content=body,
            # `is None`, not `or`: signature="" is a test asking for an unsigned
            # request, and must not quietly get a real signature.
            headers={"Stripe-Signature": _sign(body) if signature is None else signature},
        )

    return post, store


def _payer(store: JobStore, uid="usr_9", **kw) -> dict:
    store.create_user({"id": uid, "email": f"{uid}@example.com", "created_at": time.time(), **kw})
    return store.get_user_by_id(uid)


class TestWebhookRoute:
    def test_a_paid_session_grants_pro(self, webhook):
        post, store = webhook
        _payer(store)
        res = post(_session())
        assert res.status_code == 200 and res.json()["granted"] is True
        assert store.get_plan("usr_9") == "pro"

    def test_a_paid_session_records_the_payer_for_later_renewals(self, webhook):
        post, store = webhook
        _payer(store)
        post(_session(customer="cus_77"))
        assert store.get_user_by_stripe_customer("cus_77")["id"] == "usr_9"

    def test_a_renewal_regrants_through_the_customer_mapping(self, webhook):
        """The whole reason the mapping exists: this invoice names no user."""
        post, store = webhook
        _payer(store)
        post(_session(customer="cus_77"))
        store.set_plan("usr_9", "pro", time.time() - 1)  # lapsed
        assert store.get_plan("usr_9") == "free"

        res = post(_invoice(customer="cus_77"))
        assert res.json()["granted"] is True
        assert store.get_plan("usr_9") == "pro"

    def test_a_yearly_renewal_grants_a_year(self, webhook):
        post, store = webhook
        _payer(store)
        post(_session(customer="cus_77"))
        post(_invoice(customer="cus_77", interval="year"))
        user = store.get_user_by_id("usr_9")
        assert user["plan_expires_at"] > time.time() + 300 * 24 * 3600

    def test_an_unrecognised_customer_grants_nothing(self, webhook):
        """Answers 200 so Stripe stops retrying — a renewal for a customer we
        never recorded will not resolve on the tenth attempt either."""
        post, store = webhook
        _payer(store)
        res = post(_invoice(customer="cus_unknown"))
        assert res.status_code == 200 and res.json()["granted"] is False
        assert store.get_plan("usr_9") == "free"

    def test_a_bad_signature_is_refused(self, webhook):
        post, store = webhook
        _payer(store)
        res = post(_session(), signature=_sign(b"{}", "whsec_wrong"))
        assert res.status_code == 400
        assert store.get_plan("usr_9") == "free"

    def test_an_unsigned_body_is_refused(self, webhook):
        post, store = webhook
        _payer(store)
        assert post(_session(), signature="").status_code == 400
        assert store.get_plan("usr_9") == "free"

    def test_a_failed_payment_grants_nothing(self, webhook):
        post, store = webhook
        _payer(store)
        res = post({"id": "evt_9", "type": "invoice.payment_failed", "data": {"object": {}}})
        assert res.json()["granted"] is False
        assert store.get_plan("usr_9") == "free"

    def test_a_session_with_no_money_yet_grants_nothing(self, webhook):
        post, store = webhook
        _payer(store)
        res = post(_session(payment_status="unpaid"))
        assert res.json()["granted"] is False
        assert store.get_plan("usr_9") == "free"

    def test_a_session_for_an_unknown_user_grants_nothing(self, webhook):
        post, store = webhook
        res = post(_session(ref="usr_ghost__monthly"))
        assert res.status_code == 200 and res.json()["granted"] is False

    def test_an_untagged_session_grants_nothing(self, webhook):
        """Someone who paid through the bare Payment Link, bypassing our app."""
        post, store = webhook
        _payer(store)
        res = post(_session(ref=None))
        assert res.status_code == 200 and res.json()["granted"] is False
        assert store.get_plan("usr_9") == "free"

    def test_a_customer_cannot_be_claimed_by_two_accounts(self, webhook):
        """If a customer id could point at two users, a renewal could upgrade the
        wrong one."""
        post, store = webhook
        _payer(store, "usr_9")
        _payer(store, "usr_8")
        post(_session(ref="usr_9__monthly", customer="cus_77"))
        post(_session(ref="usr_8__monthly", customer="cus_77"))
        assert store.get_user_by_stripe_customer("cus_77")["id"] == "usr_8"
        assert store.get_user_by_id("usr_9")["stripe_customer_id"] is None
