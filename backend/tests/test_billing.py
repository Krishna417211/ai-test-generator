"""test_billing.py — Plan catalogue, checkout links, and webhook verification."""

import hmac
import hashlib

import pytest

from services import billing


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(billing.settings, "billing_checkout_url_monthly", "https://pay.example/m")
    monkeypatch.setattr(billing.settings, "billing_checkout_url_yearly", "https://pay.example/y?ref=1")
    monkeypatch.setattr(billing.settings, "billing_webhook_secret", "shh")


def _sign(body: bytes, secret: str = "shh") -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


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
        url = billing.checkout_url("monthly", "usr_9")
        assert "notes%5Bclient_reference_id%5D=usr_9" in url
        assert "notes%5Bplan_id%5D=monthly" in url

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
        q = parse_qs(urlparse(url).query)
        # Rebuild the notes dict the way Razorpay echoes it on the entity.
        event = {
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"notes": {
                "client_reference_id": q["notes[client_reference_id]"][0],
                "plan_id": q["notes[plan_id]"][0],
            }}}},
        }
        assert billing.extract_grant(event) == ("usr_42", "yearly")


class TestWebhookVerification:
    def test_accepts_a_correct_signature(self, configured):
        body = b'{"plan_id":"monthly"}'
        assert billing.verify_webhook(body, _sign(body)) is True

    def test_rejects_a_tampered_body(self, configured):
        assert billing.verify_webhook(b'{"plan_id":"yearly"}', _sign(b'{"plan_id":"monthly"}')) is False

    def test_rejects_a_wrong_secret(self, configured):
        body = b'{"plan_id":"monthly"}'
        assert billing.verify_webhook(body, _sign(body, "guess")) is False

    def test_rejects_a_missing_signature(self, configured):
        assert billing.verify_webhook(b"{}", "") is False

    def test_rejects_everything_when_no_secret_is_set(self, monkeypatch):
        """An unconfigured server must not accept unsigned grant requests."""
        monkeypatch.setattr(billing.settings, "billing_webhook_secret", "")
        body = b'{"plan_id":"monthly"}'
        assert billing.verify_webhook(body, _sign(body, "")) is False


def _razorpay(event: str, entity_key: str = "payment", **notes) -> dict:
    return {
        "event": event,
        "payload": {entity_key: {"entity": {"id": "pay_1", "notes": notes}}},
    }


class TestExtractGrant:
    def test_reads_the_flat_shape(self):
        assert billing.extract_grant(
            {"client_reference_id": "usr_1", "plan_id": "yearly"}
        ) == ("usr_1", "yearly")

    def test_reads_razorpay_payment_notes(self):
        event = _razorpay("payment.captured", client_reference_id="usr_1", plan_id="monthly")
        assert billing.extract_grant(event) == ("usr_1", "monthly")

    def test_reads_razorpay_subscription_notes_on_renewal(self):
        event = _razorpay("subscription.charged", "subscription",
                          client_reference_id="usr_7", plan_id="yearly")
        assert billing.extract_grant(event) == ("usr_7", "yearly")

    def test_accepts_user_id_as_a_notes_alias(self):
        event = _razorpay("payment.captured", user_id="usr_3", plan_id="monthly")
        assert billing.extract_grant(event) == ("usr_3", "monthly")

    def test_rejects_an_unknown_plan_id(self):
        _, plan = billing.extract_grant({"client_reference_id": "usr_1", "plan_id": "lifetime"})
        assert plan is None

    def test_missing_fields_yield_none(self):
        assert billing.extract_grant({}) == (None, None)

    def test_entity_without_notes_yields_none(self):
        assert billing.extract_grant(
            {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_1"}}}}
        ) == (None, None)


class TestGrantingEvents:
    @pytest.mark.parametrize("name", ["payment.captured", "subscription.charged", "order.paid"])
    def test_successful_payments_grant(self, name):
        assert billing.is_granting_event({"event": name}) is True

    @pytest.mark.parametrize("name", [
        "payment.failed", "subscription.cancelled", "subscription.halted",
        "refund.processed", "payment.authorized",
    ])
    def test_everything_else_does_not_grant(self, name):
        """A failed payment is signed just like a captured one — only the event
        name separates them, so this is what stops a free upgrade."""
        assert billing.is_granting_event({"event": name}) is False


class TestGrantSeconds:
    def test_yearly_outlasts_monthly(self):
        assert billing.grant_seconds("yearly") > billing.grant_seconds("monthly")

    def test_monthly_covers_a_long_month(self):
        assert billing.grant_seconds("monthly") >= 31 * 24 * 3600
