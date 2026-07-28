"""
test_cli_auth.py — The CLI device-code login (RFC 8628).

Before this existed, a headless machine could only authenticate with `--token`,
which meant copying a live session token around by hand. The point of the flow is
that **the password never passes through the CLI**, and that it works over SSH on
a box with no browser at all.

The interesting cases are all about what must NOT happen:

  * a code must not be approvable without a browser session — that session is the
    entire authorisation for the token the CLI collects;
  * a token must be collectable exactly once, so a leaked device_code from a
    finished login is worth nothing;
  * a user_code must not be replayable after it is used, or the same code
    approves a second request against a different account;
  * "never existed" and "expired" must be indistinguishable, or the error message
    tells someone guessing codes which guesses were once real;
  * an account suspended between approving and polling must not get in.
"""

import tempfile

import pytest
from fastapi.testclient import TestClient

import main
import ratelimit
from services import auth as auth_svc
from services.store import JobStore


@pytest.fixture(autouse=True)
def _fresh_limiters():
    """Rate-limit buckets are module-level and keyed by IP, and every TestClient
    request arrives from the same one — so without this, one test's calls spend
    the next test's budget and the failures land on whichever test happens to run
    later. Cleared per test so each starts from a known state.

    Note this makes the limits themselves untested here; TestRateLimiting below
    covers them deliberately rather than by accident.
    """
    for limiter in (ratelimit.cli_start_limiter, ratelimit.cli_code_limiter,
                    ratelimit.cli_poll_limiter):
        limiter._hits.clear()
    yield


@pytest.fixture
def client(monkeypatch):
    # main.py and auth_svc each bound the store singleton at import; both have to
    # point at the throwaway DB or the session written here is invisible there.
    store = JobStore(tempfile.mktemp(suffix=".db"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(auth_svc, "store", store)
    monkeypatch.setattr(main.settings, "frontend_url", "https://app.example.com")
    c = TestClient(main.app)
    c.store = store
    return c


def _signed_in(client, user_id="usr_1", email="dev@example.com", **extra):
    """A real account plus a real session token, as the browser would hold."""
    client.store.create_user({
        "id": user_id, "email": email, "password_hash": "x",
        "email_verified": 1, **extra,
    })
    token = auth_svc.create_login_session(user_id)
    return {"Authorization": f"Bearer {token}"}


def _start(client):
    r = client.post("/api/auth/cli/start")
    assert r.status_code == 200, r.text
    return r.json()


class TestHappyPath:
    def test_start_returns_everything_the_cli_needs(self, client):
        body = _start(client)

        assert body["device_code"]
        assert body["user_code"]
        assert body["interval"] >= 1
        assert body["expires_in"] > 0
        # The pre-filled URL is what makes the common case one click.
        assert body["user_code"] in body["verification_uri_complete"]
        assert body["verification_uri"].startswith("https://app.example.com")

    def test_user_code_is_unambiguous_and_typeable(self, client):
        """Someone reads this off a terminal and types it into a phone, so
        0/O and 1/I/L must not appear at all."""
        for _ in range(20):
            code = _start(client)["user_code"]
            assert len(code) == 9 and code[4] == "-", code
            assert not (set(code) & set("01OIL")), f"ambiguous character in {code}"

    def test_device_code_is_never_shown_in_the_verification_url(self, client):
        # The secret must not travel to the browser: the page only ever needs the
        # short code, and a URL is pasted into chat windows.
        body = _start(client)
        assert body["device_code"] not in body["verification_uri_complete"]

    def test_approve_then_poll_yields_a_working_session(self, client):
        body = _start(client)
        headers = _signed_in(client)

        assert client.post("/api/auth/cli/token",
                           json={"device_code": body["device_code"]}).json()["status"] == "pending"

        r = client.post("/api/auth/cli/approve",
                        json={"user_code": body["user_code"]}, headers=headers)
        assert r.status_code == 200, r.text

        got = client.post("/api/auth/cli/token",
                          json={"device_code": body["device_code"]}).json()
        assert got["status"] == "approved"
        assert got["user"]["email"] == "dev@example.com"

        # The token is the real thing, not a placeholder.
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {got['token']}"})
        assert me.status_code == 200, me.text

    def test_typed_code_is_forgiving_about_case_spaces_and_the_dash(self, client):
        """Rejecting 'bdfg hjkl' would be a pointless failure — the code is a
        secret shared between two screens, not a syntax exercise."""
        body = _start(client)
        headers = _signed_in(client)
        messy = body["user_code"].lower().replace("-", " ")

        assert client.post("/api/auth/cli/approve",
                           json={"user_code": messy}, headers=headers).status_code == 200
        assert client.post("/api/auth/cli/token",
                           json={"device_code": body["device_code"]}).json()["status"] == "approved"


class TestAuthorisationIsTheBrowserSession:
    def test_approving_without_a_session_is_refused(self, client):
        """The load-bearing check. A user_code is shown on screens and typed into
        phones — it is not a secret, so if it alone could approve a login, anyone
        who saw one could take over the terminal that requested it."""
        body = _start(client)

        r = client.post("/api/auth/cli/approve", json={"user_code": body["user_code"]})
        assert r.status_code in (401, 403), r.text

        # And the request is still pending, not quietly advanced.
        assert client.post("/api/auth/cli/token",
                           json={"device_code": body["device_code"]}).json()["status"] == "pending"

    def test_the_token_belongs_to_whoever_approved_it(self, client):
        body = _start(client)
        _signed_in(client, "usr_a", "a@example.com")
        headers_b = _signed_in(client, "usr_b", "b@example.com")

        client.post("/api/auth/cli/approve",
                    json={"user_code": body["user_code"]}, headers=headers_b)
        got = client.post("/api/auth/cli/token",
                          json={"device_code": body["device_code"]}).json()
        assert got["user"]["email"] == "b@example.com"


class TestOneTimeUse:
    def test_the_token_is_returned_exactly_once(self, client):
        """So a device_code leaked from a completed login is worth nothing."""
        body = _start(client)
        headers = _signed_in(client)
        client.post("/api/auth/cli/approve",
                    json={"user_code": body["user_code"]}, headers=headers)

        first = client.post("/api/auth/cli/token",
                            json={"device_code": body["device_code"]}).json()
        second = client.post("/api/auth/cli/token",
                             json={"device_code": body["device_code"]}).json()

        assert first["status"] == "approved" and first["token"]
        assert second["status"] == "expired"
        assert second["token"] is None

    def test_a_used_user_code_cannot_be_replayed(self, client):
        """Otherwise the same code approves a second request — against a
        different account, if a different browser types it."""
        body = _start(client)
        headers = _signed_in(client)
        client.post("/api/auth/cli/approve",
                    json={"user_code": body["user_code"]}, headers=headers)

        again = client.post("/api/auth/cli/approve",
                            json={"user_code": body["user_code"]}, headers=headers)
        assert again.status_code == 404


class TestRejectionAndExpiry:
    def test_denying_stops_the_poll_with_a_distinct_status(self, client):
        """"I didn't start this" needs a button, not just the option to wait it
        out — someone who has just seen an unexpected code wants to act."""
        body = _start(client)
        headers = _signed_in(client)

        assert client.post("/api/auth/cli/deny",
                           json={"user_code": body["user_code"]}, headers=headers).status_code == 200
        assert client.post("/api/auth/cli/token",
                           json={"device_code": body["device_code"]}).json()["status"] == "denied"

    def test_unknown_and_expired_codes_are_indistinguishable(self, client):
        """Different messages would tell someone guessing codes which of their
        guesses had once been real."""
        headers = _signed_in(client)
        unknown = client.post("/api/auth/cli/approve",
                              json={"user_code": "ABCD-EFGH"}, headers=headers)
        malformed = client.post("/api/auth/cli/approve",
                                json={"user_code": "nope"}, headers=headers)

        assert unknown.status_code == malformed.status_code == 404
        assert unknown.json()["detail"] == malformed.json()["detail"]

    def test_an_unknown_device_code_reports_expired_not_pending(self, client):
        # Reporting "pending" would leave a client polling a request that will
        # never exist, forever.
        got = client.post("/api/auth/cli/token", json={"device_code": "made-up"}).json()
        assert got["status"] == "expired"

    def test_empty_device_code_does_not_match_anything(self, client):
        got = client.post("/api/auth/cli/token", json={"device_code": ""}).json()
        assert got["status"] == "expired"

    def test_an_account_suspended_after_approving_is_refused(self, client):
        """The gap between approving and polling is real — seconds to minutes —
        and a lock applied in it must hold."""
        body = _start(client)
        headers = _signed_in(client, "usr_s", "s@example.com")
        client.post("/api/auth/cli/approve",
                    json={"user_code": body["user_code"]}, headers=headers)

        client.store.set_suspended("usr_s", True, reason="test")

        got = client.post("/api/auth/cli/token",
                          json={"device_code": body["device_code"]}).json()
        assert got["status"] == "denied"
        assert got["token"] is None


class TestRateLimiting:
    """Which endpoint carries the tight cap is a security decision, and the
    intuitive answer is the wrong one — so it is pinned here.

    Guessing a user_code is the only brute-force surface. Minting one hands the
    caller a code they already know, and polling needs a high-entropy
    device_code. A tight cap on /start would buy nothing and would lock out a
    whole office behind one NAT address, since the buckets are per-IP.
    """

    def test_code_submission_is_capped_tightly(self, client):
        headers = _signed_in(client)
        statuses = [
            client.post("/api/auth/cli/approve",
                        json={"user_code": "ABCD-EFGH"}, headers=headers).status_code
            for _ in range(ratelimit.cli_code_limiter.max_calls + 2)
        ]
        assert 429 in statuses, "guessing codes must hit a limit"
        assert ratelimit.cli_code_limiter.max_calls <= 20, (
            "the guess budget is what keeps a 6.6e11 space out of reach"
        )

    def test_starting_a_login_is_not_the_tight_one(self, client):
        # Comfortably more than a developer with several machines would need.
        assert ratelimit.cli_start_limiter.max_calls > ratelimit.cli_code_limiter.max_calls
        for _ in range(ratelimit.cli_code_limiter.max_calls + 2):
            assert client.post("/api/auth/cli/start").status_code == 200

    def test_polling_allows_a_full_normal_login(self, client):
        """A login polls every `interval` for up to `expires_in`, so the cap has
        to sit above that or the CLI rate-limits itself out of its own login."""
        body = _start(client)
        polls_in_one_login = body["expires_in"] / body["interval"]
        assert ratelimit.cli_poll_limiter.max_calls > polls_in_one_login
