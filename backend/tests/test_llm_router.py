"""
test_llm_router.py — Unit tests for the multi-provider rotation/failover engine.

These use asyncio.run (no pytest-asyncio plugin needed) and stub out the actual
HTTP provider calls, so nothing hits the network.
"""

import time
import asyncio

from services.llm_router import (
    LLMRouter, Provider, APIKey, ProviderState,
    RateLimitError, ProviderError, AllProvidersExhausted,
)


def _router_with(*providers: Provider) -> LLMRouter:
    """Build a router with dummy keys for the given providers (no env needed)."""
    r = LLMRouter()  # loads real env keys (may be none); we override below
    r._providers = {
        p: ProviderState(p, [APIKey(key=f"{p.value}-1", provider=p, index=1),
                              APIKey(key=f"{p.value}-2", provider=p, index=2)])
        for p in providers
    }
    return r


# ── APIKey / ProviderState ───────────────────

class TestKeyRotation:
    def test_round_robin(self):
        state = ProviderState(Provider.GEMINI, [
            APIKey("a", Provider.GEMINI, 1),
            APIKey("b", Provider.GEMINI, 2),
        ])
        first = state.next_available_key()
        second = state.next_available_key()
        third = state.next_available_key()
        assert first.index == 1 and second.index == 2 and third.index == 1

    def test_exhausted_key_skipped(self):
        state = ProviderState(Provider.GROQ, [
            APIKey("a", Provider.GROQ, 1),
            APIKey("b", Provider.GROQ, 2),
        ])
        state.keys[0].mark_exhausted(60)
        # Only key #2 is available now, twice in a row.
        assert state.next_available_key().index == 2
        assert state.next_available_key().index == 2

    def test_all_exhausted_returns_none(self):
        state = ProviderState(Provider.GROQ, [APIKey("a", Provider.GROQ, 1)])
        state.keys[0].mark_exhausted(60)
        assert state.next_available_key() is None

    def test_cooldown_expiry_restores_key(self):
        k = APIKey("a", Provider.CLAUDE, 1)
        k.mark_exhausted(60)
        assert not k.is_available
        k.exhausted_until = time.time() - 1   # simulate cooldown elapsed
        assert k.is_available


# ── Failover behaviour ───────────────────────

class TestFailover:
    def test_falls_over_to_next_provider(self):
        r = _router_with(Provider.GEMINI, Provider.GROQ)
        seen = []

        async def fake_call(provider, api_key, prompt, system, temp, json_mode=False):
            seen.append(provider)
            if provider == Provider.GEMINI:
                raise RateLimitError("429")
            return "ok-from-groq"

        r._call_provider = fake_call
        result = asyncio.run(r.complete("hi"))
        assert result == "ok-from-groq"
        assert Provider.GEMINI in seen and Provider.GROQ in seen
        # The rate-limited gemini key should now be on cooldown.
        assert not r._providers[Provider.GEMINI].keys[0].is_available

    def test_provider_error_also_fails_over(self):
        r = _router_with(Provider.GEMINI, Provider.GROQ)

        async def fake_call(provider, api_key, prompt, system, temp, json_mode=False):
            if provider == Provider.GEMINI:
                raise ProviderError("500")
            return "ok"

        r._call_provider = fake_call
        assert asyncio.run(r.complete("hi")) == "ok"

    def test_all_providers_exhausted_raises(self):
        r = _router_with(Provider.GEMINI, Provider.GROQ)

        async def fake_call(provider, api_key, prompt, system, temp, json_mode=False):
            raise RateLimitError("429")

        r._call_provider = fake_call
        try:
            asyncio.run(r.complete("hi"))
            assert False, "should have raised"
        except AllProvidersExhausted:
            pass

    def test_json_mode_is_forwarded(self):
        r = _router_with(Provider.GEMINI)
        captured = {}

        async def fake_call(provider, api_key, prompt, system, temp, json_mode=False):
            captured["json_mode"] = json_mode
            return "ok"

        r._call_provider = fake_call
        asyncio.run(r.complete("hi", json_mode=True))
        assert captured["json_mode"] is True


# ── Status reporting ─────────────────────────

class TestStatus:
    def test_status_reports_key_counts(self):
        r = _router_with(Provider.GEMINI)
        status = r.get_status()
        assert status["gemini"]["total_keys"] == 2
        assert status["gemini"]["available_keys"] == 2
        assert status["gemini"]["healthy"] is True

    def test_status_marks_unhealthy_when_all_exhausted(self):
        r = _router_with(Provider.GROQ)
        for k in r._providers[Provider.GROQ].keys:
            k.mark_exhausted(60)
        assert r.get_status()["groq"]["healthy"] is False
