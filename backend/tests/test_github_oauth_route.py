"""test_github_oauth_route.py — "Sign in with GitHub" as the way to publish.

Publishing pushes to a repo on the user's behalf, which needs a GitHub access
token. The product's answer is a GitHub sign-in, never a pasted Personal Access
Token — so the interesting cases here are the ones that used to push people back
to a token box or, worse, into a different account:

  * connecting GitHub while already signed in must attach to *that* account,
    even when the GitHub profile hides its email (which matches nothing),
  * it must keep the session the user already holds, not silently swap it,
  * it must come back to the page they started from,
  * and the session must then report itself able to push.

GitHub itself is mocked — this is our callback's logic, not a round-trip to
github.com.
"""

import time
import tempfile
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import main
from services import auth as auth_svc
from services import github_oauth
from services.store import JobStore


@pytest.fixture
def client(monkeypatch):
    # main.py and auth_svc each bound the store singleton at import; both have to
    # point at the throwaway DB or the callback writes a session the test can't see.
    store = JobStore(tempfile.mktemp(suffix=".db"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(auth_svc, "store", store)
    monkeypatch.setattr(main.settings, "github_client_id", "test-client-id")
    monkeypatch.setattr(main.settings, "github_client_secret", "test-secret")
    monkeypatch.setattr(main.settings, "frontend_url", "https://app.example.com")
    c = TestClient(main.app)
    c.store = store
    return c


def _mock_github(monkeypatch, *, uid=4242, login="octocat", email="cat@example.com",
                 token="gho_fake_token"):
    async def fake_exchange(**_kw):
        return token

    async def fake_get_user(_token):
        return {"id": uid, "login": login, "name": login, "email": email,
                "avatar_url": "https://pic"}

    monkeypatch.setattr(github_oauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(github_oauth, "get_user", fake_get_user)


def _round_trip(client, monkeypatch, *, params="", **github):
    """Drive /github/login to mint a real CSRF state, then hit the callback with
    it — the browser's actual round-trip, so the one-time state check passes."""
    _mock_github(monkeypatch, **github)
    r = client.get(f"/api/auth/github/login?{params}", follow_redirects=False)
    assert r.status_code == 307, r.text
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return client.get(f"/api/auth/github/callback?code=abc&state={state}",
                      follow_redirects=False)


def _query(resp) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(resp.headers["location"]).query).items()}


class TestConnectingFromAnExistingSession:
    """The publish page's "Connect GitHub" button, which is how a user who
    signed up with a password gets push access without ever seeing a token box."""

    def test_links_to_the_signed_in_account_despite_a_private_email(self, client, monkeypatch):
        client.store.create_user({
            "id": "usr_pw", "email": "dev@example.com", "password_hash": "x",
            "created_at": time.time(), "email_verified": True,
        })
        session = auth_svc.create_login_session("usr_pw")

        # email=None is the case that used to break this: nothing to match on, so
        # the callback minted a second account and switched the user into it.
        resp = _round_trip(client, monkeypatch, params=f"link={session}", email=None)

        assert _query(resp).get("login_error") is None
        linked = client.store.get_user_by_id("usr_pw")
        assert linked["github_login"] == "octocat"
        # And no orphan account was created alongside it.
        assert client.store.get_user_by_github("4242")["id"] == "usr_pw"

    def test_keeps_the_session_the_user_already_holds(self, client, monkeypatch):
        client.store.create_user({
            "id": "usr_pw", "email": "dev@example.com", "password_hash": "x",
            "created_at": time.time(), "email_verified": True,
        })
        session = auth_svc.create_login_session("usr_pw")

        resp = _round_trip(client, monkeypatch, params=f"link={session}", email=None)

        # Same token back: other tabs holding it stay signed in, and connecting
        # GitHub doesn't read as a logout/login cycle.
        assert _query(resp)["token"] == session
        stored = client.store.get_session(session)
        assert auth_svc.decrypt_secret(stored["github_token"]) == "gho_fake_token"

    def test_returns_to_the_page_that_sent_them(self, client, monkeypatch):
        resp = _round_trip(client, monkeypatch, params="next=/publish")
        assert _query(resp)["next"] == "/publish"

    def test_a_fragment_survives_the_round_trip(self, client, monkeypatch):
        """Connecting GitHub starts in Settings and must come back to the card
        that sent them — which is addressed by fragment (/settings#github).

        The fragment has to be carried *inside* the next parameter, encoded: a
        literal '#' in the redirect Location would be the browser's own fragment
        and the frontend would never see it as part of `next`.
        """
        resp = _round_trip(client, monkeypatch, params="next=/settings%23github")
        assert _query(resp)["next"] == "/settings#github"
        assert "%23" in resp.headers["location"]

    def test_an_identity_already_bound_elsewhere_is_not_moved(self, client, monkeypatch):
        """Signing in as a GitHub account that belongs to another user resolves
        to *that* user. Linking must not let a button press steal an identity."""
        client.store.create_user({
            "id": "usr_owner", "github_id": "4242", "github_login": "octocat",
            "created_at": time.time(), "email_verified": True,
        })
        client.store.create_user({
            "id": "usr_other", "email": "other@example.com", "password_hash": "x",
            "created_at": time.time(), "email_verified": True,
        })
        session = auth_svc.create_login_session("usr_other")

        resp = _round_trip(client, monkeypatch, params=f"link={session}", email=None)

        assert client.store.get_user_by_id("usr_other").get("github_login") is None
        # A fresh session for the account that actually owns the identity.
        assert _query(resp)["token"] != session


class TestOpenRedirect:
    """`next` is echoed into a redirect, so it is exactly the parameter an
    attacker would aim a phishing link at."""

    @pytest.mark.parametrize("evil", [
        "https://evil.example.com",
        "//evil.example.com",
        "/\\evil.example.com",
    ])
    def test_offsite_next_is_dropped(self, client, monkeypatch, evil):
        resp = _round_trip(client, monkeypatch, params=f"next={evil}")
        assert "next" not in _query(resp)


class TestSessionReportsPushAbility:
    def test_me_reports_github_connected(self, client, monkeypatch):
        resp = _round_trip(client, monkeypatch, params="next=/publish")
        token = _query(resp)["token"]
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        assert me["user"]["github_connected"] is True

    def test_password_session_on_a_github_account_is_not_connected(self, client, monkeypatch):
        """has_github and github_connected disagree here, and that is the point:
        the account has a GitHub identity, this session has no token to push
        with, so the publish page must offer to reconnect rather than promise a
        push that would 403."""
        client.store.create_user({
            "id": "usr_gh", "email": "cat@example.com", "github_id": "4242",
            "github_login": "octocat", "created_at": time.time(), "email_verified": True,
        })
        token = auth_svc.create_login_session("usr_gh")   # no github_token
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        assert me["user"]["has_github"] is True
        assert me["user"]["github_connected"] is False
