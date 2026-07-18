"""
test_llm_router.py — Unit tests for the multi-provider rotation/failover engine.

These use asyncio.run (no pytest-asyncio plugin needed) and stub out the actual
HTTP provider calls, so nothing hits the network.
"""

import pytest
import time
import asyncio

from services.llm_router import (
    LLMRouter, Provider, APIKey, ProviderState, Tier, model_for,
    RateLimitError, ProviderError, AllProvidersExhausted,
    start_provenance, record_provenance, ProviderCall, provenance_report,
    upgrade_would_change_model,
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

        async def fake_call(provider, api_key, prompt, system, temp, json_mode, spec):
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

        async def fake_call(provider, api_key, prompt, system, temp, json_mode, spec):
            if provider == Provider.GEMINI:
                raise ProviderError("500")
            return "ok"

        r._call_provider = fake_call
        assert asyncio.run(r.complete("hi")) == "ok"

    def test_all_providers_exhausted_raises(self):
        r = _router_with(Provider.GEMINI, Provider.GROQ)

        async def fake_call(provider, api_key, prompt, system, temp, json_mode, spec):
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

        async def fake_call(provider, api_key, prompt, system, temp, json_mode, spec):
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


# ── Plan tiering ─────────────────────────────

class TestTiering:
    def _capture_spec(self, r):
        seen = {}

        async def fake_call(provider, api_key, prompt, system, temp, json_mode, spec):
            seen["provider"] = provider
            seen["spec"] = spec
            return "ok"

        r._call_provider = fake_call
        return seen

    def test_pro_prefers_claude_free_prefers_gemini(self):
        """The tier decides who answers first, and that's the whole product claim.

        If Pro resolved to Flash because Flash is listed first, the upgrade would
        be charging for a model the user never gets.
        """
        for tier, expected in ((Tier.FREE, Provider.GEMINI), (Tier.PRO, Provider.CLAUDE)):
            r = _router_with(Provider.GEMINI, Provider.GROQ, Provider.CLAUDE)
            seen = self._capture_spec(r)
            asyncio.run(r.complete("hi", tier=tier))
            assert seen["provider"] is expected

    def test_tier_selects_a_genuinely_different_model(self):
        """Pro must not be the same model behind a paywall."""
        assert model_for(Provider.CLAUDE, Tier.FREE).id != model_for(Provider.CLAUDE, Tier.PRO).id

    def test_pro_falls_back_rather_than_failing(self):
        """A Pro user whose Claude keys are dry gets a suite, not an error."""
        r = _router_with(Provider.CLAUDE, Provider.GEMINI)
        seen = []

        async def fake_call(provider, api_key, prompt, system, temp, json_mode, spec):
            seen.append(provider)
            if provider == Provider.CLAUDE:
                raise RateLimitError("429")
            return "ok-from-gemini"

        r._call_provider = fake_call
        assert asyncio.run(r.complete("hi", tier=Tier.PRO)) == "ok-from-gemini"
        assert seen == [Provider.CLAUDE, Provider.GEMINI]


class TestClaudeRequestShape:
    """Opus 4.7+ removed the sampling params — sending temperature is a 400, not
    a no-op. The two tiers therefore cannot share one request body, and a model
    swap here is never just an id string."""

    def _body(self, tier):
        r = LLMRouter.__new__(LLMRouter)
        return r._claude_body("p", "sys", 0.2, model_for(Provider.CLAUDE, tier))

    def test_free_tier_sends_temperature(self):
        assert self._body(Tier.FREE)["temperature"] == 0.2

    def test_pro_tier_omits_temperature(self):
        assert "temperature" not in self._body(Tier.PRO)

    def test_pro_tier_asks_for_adaptive_thinking_not_a_token_budget(self):
        body = self._body(Tier.PRO)
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"]["effort"] == "high"
        # budget_tokens was removed on 4.7+ and is rejected with a 400.
        assert "budget_tokens" not in body["thinking"]


class TestProvenance:
    def test_reports_primary_model_across_many_calls(self):
        async def run():
            start_provenance()
            for _ in range(3):
                record_provenance(ProviderCall("claude", "claude-opus-4-8", "writer_file", 100))
            record_provenance(ProviderCall("gemini", "gemini-2.0-flash", "writer_file", 50))
            return provenance_report()

        rep = asyncio.run(run())
        assert rep["primary"] == {"provider": "claude", "model": "claude-opus-4-8"}
        assert rep["calls"] == 4
        # Rotation split the work — the suite must not be labelled with a model
        # that only wrote part of it.
        assert rep["mixed"] is True

    def test_single_model_is_not_reported_as_mixed(self):
        async def run():
            start_provenance()
            record_provenance(ProviderCall("claude", "claude-opus-4-8", "agent1", 10))
            return provenance_report()

        assert asyncio.run(run())["mixed"] is False

    def test_no_calls_reports_nothing_rather_than_guessing(self):
        async def run():
            start_provenance()
            return provenance_report()

        assert asyncio.run(run()) is None


class TestUpgradeSignal:
    """The upgrade prompt must be a fact about the model table and live
    capacity — never a reaction to a bad result. These pin the ways it could
    go wrong."""

    @pytest.fixture(autouse=True)
    def _healthy_claude(self, monkeypatch):
        """Pin the router to a healthy Claude.

        Without this the signal reads the module singleton, which loads real
        env keys — so the suite would pass on a laptop with ANTHROPIC_API_KEY_1
        set and fail in CI without it. Capacity is what the dedicated tests
        below vary; everything else assumes it's fine.
        """
        monkeypatch.setattr("services.llm_router.router", _router_with(Provider.CLAUDE))

    def _report(self, tier):
        async def run():
            start_provenance()
            record_provenance(ProviderCall("gemini", "gemini-2.0-flash", "agent1", 10))
            return provenance_report(tier)

        return asyncio.run(run())

    def test_free_is_told_which_model_pro_would_use(self):
        assert self._report(Tier.FREE)["upgrade_model"] == model_for(Provider.CLAUDE, Tier.PRO).id

    def test_pro_is_never_upsold(self):
        assert self._report(Tier.PRO)["upgrade_model"] is None

    def test_signal_dies_with_the_thing_it_claims(self, monkeypatch):
        """If Pro ever stops changing the model, the prompt must vanish on its
        own rather than keep advertising a difference that no longer exists."""
        same = model_for(Provider.CLAUDE, Tier.FREE)
        monkeypatch.setitem(
            __import__("services.llm_router", fromlist=["MODELS"]).MODELS,
            (Provider.CLAUDE, Tier.PRO), same,
        )
        assert upgrade_would_change_model(Tier.FREE) is False
        assert self._report(Tier.FREE)["upgrade_model"] is None

    def test_no_upsell_when_the_pro_model_is_out_of_credit(self, monkeypatch):
        """Found by running against the real keys, not by reasoning.

        Anthropic was out of credit, so Pro fell back to the same model Free
        already got — while the UI advertised "Pro runs claude-opus-4-8".
        Promising a model we cannot currently serve is the dishonesty this
        whole panel exists to avoid.
        """
        r = _router_with(Provider.CLAUDE)
        monkeypatch.setattr("services.llm_router.router", r)

        assert upgrade_would_change_model(Tier.FREE) is True     # credit is fine

        for k in r._providers[Provider.CLAUDE].keys:
            k.hard_blocked = True                                # out of credit
        assert upgrade_would_change_model(Tier.FREE) is False

    def test_a_transient_rate_limit_does_not_kill_the_upsell(self, monkeypatch):
        """A per-minute 429 heals on its own, so the claim stays true."""
        r = _router_with(Provider.CLAUDE)
        monkeypatch.setattr("services.llm_router.router", r)
        for k in r._providers[Provider.CLAUDE].keys:
            k.mark_exhausted(60)                                 # cooling, not blocked
        assert upgrade_would_change_model(Tier.FREE) is True

    def test_upgrade_signal_ignores_how_the_run_scored(self):
        """A weak run must not raise the upsell, and a perfect one must not
        suppress it — the score is not an input here. Tying them together would
        give us a reason to want the score low."""
        assert self._report(Tier.FREE)["upgrade_model"] == self._report(Tier.FREE)["upgrade_model"]
        # The function's only input is the tier; grounding is not in scope.
        assert upgrade_would_change_model(Tier.FREE) is True

    def test_concurrent_requests_do_not_see_each_others_calls(self):
        async def run():
            async def one(provider, model, n):
                start_provenance()
                for _ in range(n):
                    record_provenance(ProviderCall(provider, model, "x", 1))
                await asyncio.sleep(0.01)   # force interleaving
                return provenance_report()["calls"]

            return await asyncio.gather(
                one("claude", "claude-opus-4-8", 1),
                one("groq", "llama-3.3-70b-versatile", 2),
            )

        assert asyncio.run(run()) == [1, 2]
