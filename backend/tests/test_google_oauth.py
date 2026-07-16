"""test_google_oauth.py — The "Continue with Google" callback.

Google is an identity provider only, so the interesting cases are about *account
linking and trust*: a Google login must find an existing account rather than
duplicate it, must never mark an unverified address as verified, and must refuse
a suspended account the same way the GitHub path does.

Google itself is mocked — these tests exercise our callback's logic, not the
round-trip to accounts.google.com.
"""

import time
import tempfile

import pytest
from fastapi.testclient import TestClient

import main
from services import auth as auth_svc
from services import google_oauth
from services.store import JobStore


@pytest.fixture
def client(monkeypatch):
    """A TestClient over an isolated store with Google OAuth enabled.

    main.py and auth_svc each bind the store singleton by name at import, so both
    references have to point at the throwaway instance or a session written by the
    callback lands in a different database than the one the test reads.
    """
    store = JobStore(tempfile.mktemp(suffix=".db"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(auth_svc, "store", store)
    monkeypatch.setattr(main.settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(main.settings, "google_client_secret", "test-secret")
    monkeypatch.setattr(main.settings, "frontend_url", "https://app.example.com")
    c = TestClient(main.app)
    c.store = store  # let tests reach the same store
    return c


def _mock_google(monkeypatch, *, sub="google-sub-1", email="user@example.com",
                 email_verified=True, name="Real User"):
    async def fake_exchange(**_kw):
        return "fake-access-token"

    async def fake_get_user(_token):
        return {"id": sub, "email": email, "email_verified": email_verified,
                "name": name, "avatar_url": "https://pic"}

    monkeypatch.setattr(google_oauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(google_oauth, "get_user", fake_get_user)


def _login_then_callback(client, monkeypatch, **google):
    """Drive /google/login to mint a real CSRF state, then hit the callback with
    it — exactly the browser's round-trip, so the one-time state check passes."""
    _mock_google(monkeypatch, **google)
    r = client.get("/api/auth/google/login", follow_redirects=False)
    assert r.status_code == 307
    state = r.headers["location"].split("state=")[1].split("&")[0]
    return client.get(
        f"/api/auth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )


def _redirect_token(resp) -> str | None:
    loc = resp.headers["location"]
    return loc.split("token=")[1] if "token=" in loc else None


def _redirect_error(resp) -> str | None:
    loc = resp.headers["location"]
    return loc.split("login_error=")[1] if "login_error=" in loc else None


class TestNewAccount:
    def test_creates_verified_account_and_issues_token(self, client, monkeypatch):
        resp = _login_then_callback(client, monkeypatch, email="new@example.com")
        assert resp.status_code == 307
        assert _redirect_token(resp)  # a session token came back
        user = client.store.get_user_by_email("new@example.com")
        assert user and user["google_id"] == "google-sub-1"
        assert user["email_verified"] is True


class TestReturningAndLinking:
    def test_returning_user_matches_by_google_id(self, client, monkeypatch):
        first = _login_then_callback(client, monkeypatch, email="a@example.com")
        original_id = client.store.get_user_by_email("a@example.com")["id"]
        assert _redirect_token(first)
        # Second login, same sub but Google now reports a different email. It must
        # resolve to the SAME account by google_id — not mint a second one — and
        # the account keeps its original email rather than being rewritten.
        _login_then_callback(client, monkeypatch, email="changed@example.com")
        assert client.store.get_user_by_google("google-sub-1")["id"] == original_id
        assert client.store.get_user_by_email("changed@example.com") is None

    def test_links_onto_existing_password_account(self, client, monkeypatch):
        client.store.create_user({
            "id": "usr_pw", "email": "dev@example.com", "password_hash": "x",
            "created_at": time.time(), "email_verified": False,
        })
        resp = _login_then_callback(client, monkeypatch, email="dev@example.com")
        assert _redirect_token(resp)
        linked = client.store.get_user_by_id("usr_pw")
        assert linked["google_id"] == "google-sub-1"
        assert linked["email_verified"] is True  # Google proved the inbox


class TestTrustBoundaries:
    def test_unverified_google_email_is_refused(self, client, monkeypatch):
        resp = _login_then_callback(client, monkeypatch, email_verified=False)
        assert _redirect_error(resp) == "email_unverified"
        assert client.store.get_user_by_email("user@example.com") is None

    def test_suspended_account_cannot_log_in(self, client, monkeypatch):
        client.store.create_user({
            "id": "usr_s", "email": "user@example.com", "google_id": "google-sub-1",
            "created_at": time.time(), "email_verified": True, "suspended": True,
        })
        resp = _login_then_callback(client, monkeypatch)
        assert _redirect_error(resp) == "suspended"
        assert _redirect_token(resp) is None

    def test_bad_state_is_rejected(self, client, monkeypatch):
        _mock_google(monkeypatch)
        resp = client.get(
            "/api/auth/google/callback?code=abc&state=forged",
            follow_redirects=False,
        )
        assert _redirect_error(resp) == "invalid_state"

    def test_disabled_when_unconfigured(self, client, monkeypatch):
        monkeypatch.setattr(main.settings, "google_client_id", "")
        resp = client.get("/api/auth/google/login", follow_redirects=False)
        assert resp.status_code == 503
