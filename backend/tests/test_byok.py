"""
test_byok.py — Bring your own Gemini key.

The feature exists so a user's generations run on their own API quota rather than
the operator's. That makes three things load-bearing, and they are what these
tests are about:

  1. The key is a credential belonging to someone else. It must be encrypted at
     rest and must never come back out of an endpoint — only a mask.
  2. A user's key must not be able to affect anyone else. A revoked or throttled
     key cannot be allowed to mark the shared Gemini pool as cooling.
  3. The accounting has to be honest in both directions: a run on the user's own
     key costs them no quota credit, and a run that fell back to the operator's
     pool does — because that one really did cost the operator.
"""

import tempfile

import pytest
from fastapi.testclient import TestClient

import main
import ratelimit
from services import auth as auth_svc
from services import llm_router as llm_router_mod
from services.llm_router import APIKey, Provider
from services.store import JobStore

GOOD_KEY = "AIzaSyC" + "a" * 30 + "9f2k"


@pytest.fixture(autouse=True)
def _fresh_limiters():
    for name in dir(ratelimit):
        lim = getattr(ratelimit, name)
        if isinstance(lim, ratelimit.SlidingWindowLimiter):
            lim._hits.clear()
    yield


@pytest.fixture
def client(monkeypatch):
    store = JobStore(tempfile.mktemp(suffix=".db"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(auth_svc, "store", store)
    monkeypatch.setattr(main.settings, "session_secret", "test-secret-for-encryption")
    c = TestClient(main.app)
    c.store = store
    return c


def _signed_in(client, user_id="u1"):
    # Email derived from the id: the users table has a UNIQUE constraint on it,
    # so a fixed address makes any test with two accounts fail on setup.
    client.store.create_user({"id": user_id, "email": f"{user_id}@example.com",
                              "password_hash": "x", "email_verified": 1})
    return {"Authorization": f"Bearer {auth_svc.create_login_session(user_id)}"}


def _accept_probe(monkeypatch, ok=True, why="rejected"):
    async def probe(_key):
        return (ok, "" if ok else why)
    monkeypatch.setattr(main.llm_router, "probe_user_key", probe, raising=False)
    monkeypatch.setattr(llm_router_mod, "probe_user_key", probe, raising=False)


class TestTheKeyNeverComesBack:
    def test_saving_a_key_returns_only_a_mask(self, client, monkeypatch):
        _accept_probe(monkeypatch)
        headers = _signed_in(client)

        r = client.put("/api/settings", json={"gemini_api_key": GOOD_KEY}, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["has_gemini_key"] is True
        assert body["gemini_key_hint"] == "AIza…9f2k"
        assert GOOD_KEY not in r.text, "the key must never appear in a response"
        assert "gemini_api_key" not in body

    def test_reading_settings_never_returns_the_key(self, client, monkeypatch):
        _accept_probe(monkeypatch)
        headers = _signed_in(client)
        client.put("/api/settings", json={"gemini_api_key": GOOD_KEY}, headers=headers)

        r = client.get("/api/settings", headers=headers)
        assert GOOD_KEY not in r.text
        assert r.json()["gemini_key_hint"] == "AIza…9f2k"

    def test_the_key_is_encrypted_at_rest(self, client, monkeypatch):
        """A leaked database must not be a list of working API keys — the same
        standard the session tokens are held to."""
        _accept_probe(monkeypatch)
        headers = _signed_in(client)
        client.put("/api/settings", json={"gemini_api_key": GOOD_KEY}, headers=headers)

        stored = client.store.get_gemini_key("u1")
        assert stored, "something should be stored"
        assert stored != GOOD_KEY, "the key must not be stored in the clear"
        assert auth_svc.decrypt_secret(stored) == GOOD_KEY, "and must decrypt back"

    def test_the_mask_shows_both_ends_and_nothing_between(self, client):
        hint = client.store.gemini_key_hint(GOOD_KEY)
        assert hint.startswith("AIza"), "the prefix confirms it's a Google key"
        assert hint.endswith("9f2k"), "the suffix distinguishes two keys"
        assert GOOD_KEY[8:-8] not in hint, "the middle must never appear"

    def test_no_key_means_no_hint(self, client):
        headers = _signed_in(client)
        body = client.get("/api/settings", headers=headers).json()
        assert body["has_gemini_key"] is False
        assert body["gemini_key_hint"] == ""


class TestSavingAndClearing:
    def test_a_bad_key_is_refused_before_it_is_stored(self, client, monkeypatch):
        """Rejected at the form, not in the middle of a generation minutes later
        where nobody would connect the error back to this field."""
        _accept_probe(monkeypatch, ok=False, why="Google rejected it.")
        headers = _signed_in(client)

        r = client.put("/api/settings", json={"gemini_api_key": GOOD_KEY}, headers=headers)
        assert r.status_code == 400
        assert "didn't work" in r.json()["detail"]
        assert client.store.get_gemini_key("u1") == "", "nothing should have been stored"

    def test_another_provider_s_key_is_caught_by_shape(self, client, monkeypatch):
        _accept_probe(monkeypatch)
        headers = _signed_in(client)
        r = client.put("/api/settings",
                       json={"gemini_api_key": "sk-ant-abcdefghijklmnopqrstuvwxyz"},
                       headers=headers)
        assert r.status_code == 422
        assert "AIza" in r.text

    def test_an_empty_string_clears_it(self, client, monkeypatch):
        _accept_probe(monkeypatch)
        headers = _signed_in(client)
        client.put("/api/settings", json={"gemini_api_key": GOOD_KEY}, headers=headers)

        r = client.put("/api/settings", json={"gemini_api_key": ""}, headers=headers)
        assert r.json()["has_gemini_key"] is False
        assert client.store.get_gemini_key("u1") == ""

    def test_saving_other_settings_does_not_clear_the_key(self, client, monkeypatch):
        """Omission means "leave it alone". Otherwise changing your default
        framework would silently drop your API key."""
        _accept_probe(monkeypatch)
        headers = _signed_in(client)
        client.put("/api/settings", json={"gemini_api_key": GOOD_KEY}, headers=headers)

        r = client.put("/api/settings", json={"framework": "cypress"}, headers=headers)
        assert r.json()["has_gemini_key"] is True
        assert r.json()["framework"] == "cypress"

    def test_the_key_is_scoped_to_the_account(self, client, monkeypatch):
        _accept_probe(monkeypatch)
        a = _signed_in(client, "u1")
        client.put("/api/settings", json={"gemini_api_key": GOOD_KEY}, headers=a)

        b = _signed_in(client, "u2")
        assert client.get("/api/settings", headers=b).json()["has_gemini_key"] is False


class TestIsolationFromTheSharedPool:
    """One user's bad key must not degrade generation for everyone else."""

    def test_a_user_key_is_never_added_to_the_shared_pool(self):
        router = llm_router_mod.router
        before = {p: [k.key for k in st.keys] for p, st in router._providers.items()}

        key = APIKey(key=GOOD_KEY, provider=Provider.GEMINI, index=0)
        key.mark_exhausted(600)          # as a 429 on the user's key would

        after = {p: [k.key for k in st.keys] for p, st in router._providers.items()}
        assert before == after, "the shared pool must be untouched"
        assert all(GOOD_KEY not in keys for keys in after.values())

    def test_exhausting_a_user_key_leaves_shared_keys_available(self):
        """The failure this prevents: a throttled user key marking the operator's
        Gemini keys as cooling, so everyone else falls through to Groq."""
        router = llm_router_mod.router
        state = router._providers.get(Provider.GEMINI)
        if not state or not state.keys:
            pytest.skip("no Gemini keys configured in this environment")

        shared_available_before = [k.is_available for k in state.keys]
        user = APIKey(key=GOOD_KEY, provider=Provider.GEMINI, index=0)
        user.mark_exhausted(600)

        assert [k.is_available for k in state.keys] == shared_available_before


class TestFallbackAccounting:
    """The honesty requirement, in both directions."""

    def test_the_flag_starts_clear(self):
        llm_router_mod.start_byok_tracking()
        assert llm_router_mod.byok_fell_back() is False

    def test_falling_back_is_recorded(self):
        llm_router_mod.start_byok_tracking()
        llm_router_mod._byok_fallback.set(True)
        assert llm_router_mod.byok_fell_back() is True

    def test_tracking_resets_between_requests(self):
        """Otherwise one user's fallback would charge the next user's run."""
        llm_router_mod.start_byok_tracking()
        llm_router_mod._byok_fallback.set(True)
        llm_router_mod.start_byok_tracking()
        assert llm_router_mod.byok_fell_back() is False


class TestProbe:
    def test_a_throttled_key_is_accepted(self, monkeypatch):
        """429 is a fact about right now, not about the key. Rejecting it would
        tell the user their perfectly good key is broken."""
        async def rate_limited(*a, **k):
            raise llm_router_mod.RateLimitError("429")
        monkeypatch.setattr(llm_router_mod.router, "_call_gemini", rate_limited)

        import asyncio
        ok, _ = asyncio.run(llm_router_mod.probe_user_key(GOOD_KEY))
        assert ok is True

    def test_a_rejected_key_is_refused_with_a_useful_reason(self, monkeypatch):
        async def refused(*a, **k):
            raise llm_router_mod.ProviderError("API key not valid. Please pass a valid API key.")
        monkeypatch.setattr(llm_router_mod.router, "_call_gemini", refused)

        import asyncio
        ok, why = asyncio.run(llm_router_mod.probe_user_key(GOOD_KEY))
        assert ok is False
        assert "Generative Language API" in why, "should say what to check"

    def test_a_network_failure_does_not_condemn_the_key(self, monkeypatch):
        async def boom(*a, **k):
            raise OSError("dns")
        monkeypatch.setattr(llm_router_mod.router, "_call_gemini", boom)

        import asyncio
        ok, why = asyncio.run(llm_router_mod.probe_user_key(GOOD_KEY))
        assert ok is False
        assert "Couldn't reach Google" in why, "must not claim the key is bad"
