"""
llm_router.py — Multi-provider LLM rotation engine for Testra

Strategy:
  1. Try providers in priority order (Gemini → Groq → Claude)
  2. Within each provider, rotate through all API keys round-robin
  3. On rate limit (429) or error: exponential backoff then next key/provider
  4. Track exhausted keys per reset window, auto-restore after cooldown
  5. Stream real-time provider status back to the frontend via SSE

All providers are FREE tier — no credit card required.
"""

import os
import re
import ssl
import time
import asyncio
import logging
import contextvars
from enum import Enum
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

import httpx
from dotenv import load_dotenv

# Load backend/.env so API keys are available when running manually
# (`uvicorn main:app`). In Docker the vars are already injected via env_file;
# load_dotenv does not override existing environment variables, so both work.
load_dotenv()

logger = logging.getLogger(__name__)

# Per-request status listener. Each SSE request is driven in its own asyncio
# task, so a ContextVar isolates provider-status updates to the stream that
# triggered them — no cross-talk between concurrent users, and no shared
# mutable collection to iterate while it's being modified.
_status_listener: contextvars.ContextVar = contextvars.ContextVar(
    "testgen_status_listener", default=None
)

# Token usage from the most recent provider call, scoped per-request (per asyncio
# task) so concurrent requests never read each other's counts. Each _call_*
# implementation sets it after parsing the response; complete() reads it for the
# call log. None means "provider didn't report usage" (e.g. streaming).
_last_usage: contextvars.ContextVar = contextvars.ContextVar(
    "testgen_last_usage", default=None
)

# Every provider call made while serving the current request, in order. Scoped
# per asyncio task for the same reason as _last_usage: concurrent users must not
# read each other's calls.
#
# This accumulates rather than recording only the latest call because one user
# -visible output is not one call — a generate is the filter agent, a file plan,
# and one call per file, and rotation can serve them from different providers.
# "Which model wrote my tests?" therefore only has an honest answer in aggregate,
# which is what report() computes.
_provenance: contextvars.ContextVar = contextvars.ContextVar(
    "testgen_provenance", default=None
)


def _openai_usage(usage: Optional[dict]) -> Optional[dict]:
    """Normalise an OpenAI-style usage block (Groq) to {prompt, completion, total}."""
    if not usage:
        return None
    return {
        "prompt": usage.get("prompt_tokens"),
        "completion": usage.get("completion_tokens"),
        "total": usage.get("total_tokens"),
    }

# ─────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────

class Provider(str, Enum):
    GEMINI   = "gemini"
    GROQ     = "groq"
    CLAUDE   = "claude"


# The env var each provider's keys are read from, numbered _1.._10. Named once
# here because two things need it: loading the keys, and telling an admin which
# variable to rotate when one goes bad (see get_key_health).
_ENV_PREFIXES: dict[Provider, str] = {
    Provider.GEMINI: "GEMINI_API_KEY_",
    Provider.GROQ:   "GROQ_API_KEY_",
    Provider.CLAUDE: "ANTHROPIC_API_KEY_",
}


# A provider telling us when to come back is the strongest possible evidence the
# limit is temporary — stronger than anything we can infer from its prose.
# Matches Gemini's `"retryDelay": "21s"` and Groq's `try again in 1h26m46.464s`.
_RETRY_HINT_RE = re.compile(
    r'retrydelay"?\s*:\s*"?(\d[\dhms.]*)|try again in\s+(\d[\dhms.]*)', re.IGNORECASE
)

# Spent for the day/project and no retry hint offered. Waiting still fixes these
# eventually, but not within a request, so they count as capacity being gone.
_DAILY_EXHAUSTED_MARKERS = ("perdayperproject", "requestsperday")

# The account cannot serve *any* request until a human acts. Only these justify
# telling a user that capacity is genuinely gone.
_OUT_OF_CREDIT_MARKERS = ("credit balance is too low", "insufficient_quota")


def is_hard_quota(text: str) -> bool:
    """True when waiting will NOT bring this key back.

    Biased deliberately toward "recoverable". A false negative costs one wasted
    retry; a false positive marks the key dead for the life of the process
    (`hard_blocked` clears only on a success), tells the user "out of credit"
    for what may be a 21-second wait, and silently drops the Pro upgrade prompt
    via `upgrade_would_change_model`.

    The previous test — `"billing" in t and "quota" in t` — did exactly that.
    Gemini appends "please check your plan and billing details" to *every* 429,
    including the per-minute ones, so a routine rate limit permanently disabled
    Gemini and produced an "every provider is out of credit" message while the
    response's own `retryDelay` said 21 seconds.
    """
    t = text.lower()
    if any(m in t for m in _OUT_OF_CREDIT_MARKERS):
        return True                       # no amount of waiting adds money
    if _RETRY_HINT_RE.search(t):
        return False                      # the provider itself says it recovers
    return any(m in t.replace("_", "") for m in _DAILY_EXHAUSTED_MARKERS)


@dataclass
class APIKey:
    key: str
    provider: Provider
    index: int                          # position in the key pool
    call_count: int = 0
    error_count: int = 0
    exhausted_until: float = 0.0       # epoch seconds — 0 means available
    last_used: float = 0.0
    hard_blocked: bool = False         # out of credit / daily quota, not a 429

    @property
    def is_available(self) -> bool:
        return time.time() >= self.exhausted_until

    def mark_exhausted(self, cooldown_seconds: int = 60):
        self.exhausted_until = time.time() + cooldown_seconds
        self.error_count += 1
        logger.warning(
            f"[{self.provider}] key #{self.index} exhausted — "
            f"cooling down for {cooldown_seconds}s"
        )

    def record_success(self):
        self.call_count += 1
        self.last_used = time.time()
        self.error_count = 0          # reset on success
        self.hard_blocked = False     # credit was topped up / quota reset


@dataclass
class ProviderState:
    provider: Provider
    keys: list[APIKey]
    current_index: int = 0            # round-robin pointer

    def next_available_key(self) -> Optional[APIKey]:
        """Round-robin through keys, skipping exhausted ones."""
        n = len(self.keys)
        for _ in range(n):
            key = self.keys[self.current_index]
            self.current_index = (self.current_index + 1) % n
            if key.is_available:
                return key
        return None  # all keys for this provider are exhausted


# ─────────────────────────────────────────────
# Provider implementations
# ─────────────────────────────────────────────

class Tier(str, Enum):
    """Which model quality a request is entitled to.

    This is the *only* thing that differs between plans inside the router, and
    it exists so the upgrade prompt can tell the truth: a Pro generation really
    is produced by a stronger model, not the same one behind a paywall. Keep it
    that way — if Pro ever stops changing the model, the claim has to go too.
    """
    FREE = "free"
    PRO  = "pro"


@dataclass(frozen=True)
class ModelSpec:
    """One provider's model for one tier, plus what its API will accept.

    The per-model flags are not decoration. Anthropic removed the sampling
    parameters on Opus 4.7+, so sending `temperature` — which every call did
    when Claude meant Haiku — returns a 400 rather than being ignored. A model
    swap here is therefore never just an id string, and the request builders
    read these flags instead of assuming one shape for the whole provider.
    """
    id: str
    max_output_tokens: int
    # Opus 4.7+ reject temperature/top_p/top_k outright (400). Haiku accepts them.
    accepts_temperature: bool = True
    # Adaptive thinking is off unless asked for on Opus 4.8, and it is most of
    # what the Pro tier is actually buying: the model reasons about the repo
    # before writing selectors instead of pattern-matching to a generic suite.
    # `budget_tokens` is removed on 4.7+ — depth is set with `effort`, not tokens.
    adaptive_thinking: bool = False
    effort: Optional[str] = None          # low | medium | high | xhigh | max


# The model each provider runs, per tier. Two rows differ between the tiers and
# both are Anthropic's, because that is where a materially stronger model is
# actually available to us: Gemini and Groq stay on their free-tier models and
# serve as capacity, not as the quality story.
# A Gemini model swap here is not cosmetic: Google zeroed the free-tier quota for
# gemini-2.0-flash, and a model with no allocation fails as `limit: 0` on the
# per-minute metric rather than as a used-up daily cap. That reads exactly like
# exhaustion, so it burns both keys and their cooldowns on every call before
# falling through — while looking like traffic we caused. If Gemini starts
# failing wholesale on a fresh day, check the quota metric for `limit: 0` before
# assuming usage.
#
# The model must also be one that EVERY Gemini key can serve, which is a
# stricter test than "it works". Keys live in different Google projects, and a
# project created after a model closed to new users gets a 404 for it forever
# — `gemini-2.5-flash` works on the older project here and 404s on the newer
# one. A per-(provider, tier) model is shared by every key in rotation, so
# picking one grandfathered into only some projects silently strands the rest.
# Verified on both keys before changing. Avoid `-preview` (withdrawn without
# notice) and `-latest` (drifts under you, changing output with no code change).
MODELS: dict[tuple[Provider, Tier], ModelSpec] = {
    (Provider.GEMINI, Tier.FREE): ModelSpec("gemini-3.6-flash", 8192),
    (Provider.GEMINI, Tier.PRO):  ModelSpec("gemini-3.6-flash", 8192),
    (Provider.GROQ,   Tier.FREE): ModelSpec("llama-3.3-70b-versatile", 4096),
    (Provider.GROQ,   Tier.PRO):  ModelSpec("llama-3.3-70b-versatile", 4096),
    (Provider.CLAUDE, Tier.FREE): ModelSpec("claude-haiku-4-5", 4096),
    # Sonnet rather than Opus: a test suite is bounded, well-specified work, and
    # Sonnet 5 clears it at $3/$15 per MTok against Opus 4.8's $5/$25 — ~40%
    # cheaper, so a fixed credit balance goes ~1.7x further (~2.5x while Sonnet 5
    # introductory pricing lasts). Opus is the better model, but not by enough
    # here to justify draining the Pro tier's balance that much faster — running
    # out of credit is what actually takes the provider off the board. Same
    # request surface as Opus 4.7+: sampling params are rejected and thinking
    # depth is set with `effort`, so the flags below are unchanged.
    (Provider.CLAUDE, Tier.PRO):  ModelSpec(
        "claude-sonnet-5",
        max_output_tokens=16000,   # non-streaming ceiling that stays under the HTTP timeout
        accepts_temperature=False,
        adaptive_thinking=True,
        effort="high",
    ),
}

# Which provider to try first, per tier. Free order is capacity-first (Gemini's
# 1M context swallows big repos, Groq is fastest). Pro order is quality-first:
# paying for Opus and then serving the request from Flash because Flash was
# listed first would make the upgrade a lie. Claude still falls back to the
# others rather than failing — a Pro user who would otherwise get nothing gets
# a free-tier-quality suite, and the provenance report says so.
PROVIDER_PRIORITY: dict[Tier, list[Provider]] = {
    Tier.FREE: [Provider.GEMINI, Provider.GROQ, Provider.CLAUDE],
    Tier.PRO:  [Provider.CLAUDE, Provider.GEMINI, Provider.GROQ],
}

# How long to wait before retrying an exhausted key (seconds)
COOLDOWN = {
    Provider.GEMINI:   60,
    Provider.GROQ:     30,
    Provider.CLAUDE:   120,
}

# A key that failed to *connect* is not rate-limited and has consumed nothing, so
# it comes back quickly — long enough only to prefer a different key on the next
# attempt rather than hammering a socket that just broke.
TRANSPORT_COOLDOWN = 15

# How many times to reconnect on the same key before giving up on it. A TLS or
# connection failure is usually per-socket and clears on the next attempt, so a
# couple of quick retries recover the call without touching another provider's
# quota — which matters most when the other providers are exactly the ones
# already dry.
TRANSPORT_RETRIES = 2
TRANSPORT_RETRY_DELAY = 1.5


def model_for(provider: Provider, tier: Tier) -> ModelSpec:
    return MODELS[(provider, tier)]


# ─────────────────────────────────────────────
# Provenance — which model actually produced an output
# ─────────────────────────────────────────────

@dataclass
class ProviderCall:
    """One successful provider call made while producing a user-visible output."""
    provider: str
    model: str
    context: str                    # "agent1" | "writer_file" | "scan_summary" | ...
    total_tokens: Optional[int] = None


# Set when a request that supplied its own API key had to fall back to the
# shared pool. The caller needs to know for two reasons that are both about
# fairness rather than mechanics: the fallback consumed the operator's capacity,
# so it must cost the user a quota credit, and the user asked to run on their own
# key, so they are owed an explanation of why they didn't.
#
# A ContextVar rather than a return value because `complete()` returns the
# model's text and is called from a dozen places; threading a tuple through all
# of them to report a condition almost nobody hits would be worse.
_byok_fallback: contextvars.ContextVar = contextvars.ContextVar(
    "testgen_byok_fallback", default=False
)


def start_byok_tracking() -> None:
    """Reset the fallback flag. Call once at the start of a request."""
    _byok_fallback.set(False)


def byok_fell_back() -> bool:
    """True if a user-key request ended up spending the operator's capacity."""
    return bool(_byok_fallback.get())


def start_provenance() -> None:
    """Begin recording provider calls for the current request.

    Call once at the top of a request that produces a user-visible output.
    """
    _provenance.set([])


def record_provenance(call: ProviderCall) -> None:
    calls = _provenance.get()
    if calls is not None:
        calls.append(call)


def upgrade_would_change_model(tier: Tier) -> bool:
    """Whether upgrading would genuinely get this user a stronger model *today*.

    Two conditions, and both are load-bearing:

    1. The model table must actually differ between the tiers. Derived rather
       than hardcoded, so if Pro ever stops changing the model this goes False
       on its own — the claim cannot outlive the thing it claims.

    2. The Pro provider must be reachable. This one was added after running it
       against the real keys: Anthropic was out of credit, so Pro resolved to
       the same Groq model Free was already getting — while the UI cheerfully
       advertised the Pro model by name. Selling an upgrade to a model we
       cannot currently serve is the exact dishonesty this panel exists to
       avoid, and no unit test would have caught it.

    Note what this is NOT keyed on: the grounding score. A low selector rate
    means the repo has few stable selectors, which a better model cannot
    invent — offering an upgrade there would be selling a fix we don't have,
    and would give us a reason to want the score low.
    """
    if tier is not Tier.FREE:
        return False
    if model_for(Provider.CLAUDE, Tier.PRO).id == model_for(Provider.CLAUDE, Tier.FREE).id:
        return False
    return router.provider_is_viable(Provider.CLAUDE)


def provenance_report(tier: Tier = Tier.FREE) -> Optional[dict]:
    """Aggregate this request's provider calls into a reportable shape.

    Returns None when nothing was recorded (no LLM ran, or the caller never
    started recording) — the caller must then say nothing rather than guess.

    `primary` is decided by call count and ties break toward the more-capable
    model, which matters because it is what the UI labels the output with.
    Deliberately reports provider + model and never the key: which of the
    numbered keys served a call is an operations detail (see get_key_health),
    and putting it on a user-facing response would leak the pool's shape into
    screenshots and bug reports for no user benefit.
    """
    calls = _provenance.get()
    if not calls:
        return None

    by_model: dict[tuple[str, str], dict] = {}
    for c in calls:
        key = (c.provider, c.model)
        entry = by_model.setdefault(
            key, {"provider": c.provider, "model": c.model, "calls": 0, "total_tokens": 0}
        )
        entry["calls"] += 1
        entry["total_tokens"] += c.total_tokens or 0

    models = sorted(by_model.values(), key=lambda m: m["calls"], reverse=True)
    for m in models:
        m["share"] = round(m["calls"] / len(calls), 3)
        # A provider that reports no usage (streaming) would otherwise show a
        # confident 0 rather than "not reported".
        if m["total_tokens"] == 0:
            m["total_tokens"] = None

    return {
        "calls": len(calls),
        "primary": {"provider": models[0]["provider"], "model": models[0]["model"]},
        # Present whenever rotation split the work, so a suite that fell back
        # mid-run isn't labelled with a model that only wrote part of it.
        "mixed": len(models) > 1,
        "models": models,
        # A statement of fact about the model table, not a reaction to how this
        # particular run scored. Only reachable when calls succeeded, so a
        # provider outage — which hits paid users identically — can never
        # surface as an upsell (see config.free_generations_per_month).
        "upgrade_model": (
            model_for(Provider.CLAUDE, Tier.PRO).id
            if upgrade_would_change_model(tier) else None
        ),
    }


class LLMRouter:
    """
    Manages API key rotation across all free LLM providers.
    Thread-safe via asyncio locks — safe for concurrent FastAPI requests.
    """

    def __init__(self):
        self._providers: dict[Provider, ProviderState] = {}
        self._lock = asyncio.Lock()
        self._call_log: list[dict] = []      # in-memory audit log
        self._load_keys()

    def _load_keys(self):
        """Load all API keys from environment variables."""
        for provider, prefix in _ENV_PREFIXES.items():
            keys = []
            for i in range(1, 11):  # support up to 10 keys per provider
                val = os.getenv(f"{prefix}{i}")
                if val:
                    keys.append(APIKey(key=val, provider=provider, index=i))
            if keys:
                self._providers[provider] = ProviderState(
                    provider=provider, keys=keys
                )
                logger.info(f"[{provider}] Loaded {len(keys)} API key(s)")
            else:
                logger.warning(f"[{provider}] No API keys found — provider disabled")

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    async def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.2,
        stream: bool = False,
        context_hint: str = "",     # used for logging ("agent1" | "agent2")
        json_mode: bool = False,    # force the provider to emit valid JSON
        tier: Tier = Tier.FREE,     # which model quality the caller is entitled to
        user_key: str = "",         # the caller's OWN Gemini key ("bring your own key")
    ) -> str:
        """
        Try each provider in priority order until one succeeds.
        Returns the complete LLM response as a string.

        `user_key`, when supplied, is tried FIRST and on its own. It is the whole
        point of the bring-your-own-key option: a user who supplied a key expects
        their work to run on their quota, not the operator's. Only if that key
        fails does the shared pool get used, and that fallback is recorded (see
        `byok_fell_back`) so it can be charged and explained rather than passing
        silently — silence here would rebuild the exact cost the option exists to
        remove, and hide it.
        """
        last_error = None
        cooling: list[str] = []     # providers skipped because every key is in cooldown

        if user_key:
            try:
                return await self._call_with_user_key(
                    user_key, prompt, system_prompt, temperature, json_mode, tier,
                    context_hint,
                )
            except Exception as e:
                # Their key is unusable right now — rate-limited, revoked, out of
                # credit, or simply mistyped. Fall through to the shared pool and
                # flag it. Deliberately catching broadly: whatever went wrong with
                # a third party's key, the run should still complete.
                logger.warning(f"User-supplied Gemini key failed ({e!r}) — falling back to the shared pool")
                _byok_fallback.set(True)
                last_error = e

        for provider in PROVIDER_PRIORITY[tier]:
            if provider not in self._providers:
                continue

            state = self._providers[provider]

            async with self._lock:
                api_key = state.next_available_key()

            if api_key is None:
                logger.info(f"[{provider}] All keys exhausted — skipping")
                cooling.append(provider.value)
                continue

            spec = model_for(provider, tier)
            await self._broadcast_status(f"Using {spec.id}...")

            _last_usage.set(None)
            t0 = time.perf_counter()
            try:
                result = await self._retry_transport(
                    provider, api_key, prompt, system_prompt, temperature, json_mode, spec
                )
                latency_ms = round((time.perf_counter() - t0) * 1000)
                api_key.record_success()
                usage = _last_usage.get()
                self._log_call(provider, api_key, context_hint, success=True,
                               latency_ms=latency_ms, usage=usage, model=spec.id)
                record_provenance(ProviderCall(
                    provider=provider.value,
                    model=spec.id,
                    context=context_hint,
                    total_tokens=(usage or {}).get("total"),
                ))
                return result

            except RateLimitError as e:
                logger.warning(f"[{provider}] Rate limited: {e}")
                api_key.hard_blocked = is_hard_quota(str(e))
                api_key.mark_exhausted(COOLDOWN[provider])
                last_error = e
                continue

            except ProviderError as e:
                logger.error(f"[{provider}] Error: {e}")
                api_key.hard_blocked = is_hard_quota(str(e))
                api_key.mark_exhausted(COOLDOWN[provider] // 2)
                last_error = e
                continue

            except (httpx.HTTPError, ssl.SSLError) as e:
                # A transport failure — TLS handshake, reset connection, read
                # timeout — says nothing about the request, and every other key
                # and provider may well be reachable. Without this branch it
                # escaped `complete()` raw: no rotation, no fallback, the whole
                # call dead because one socket to one key misbehaved. Observed as
                # `SSLError: RECORD_LAYER_FAILURE` losing one file of a suite and
                # taking the CI pipeline down with it (the suite was then
                # incomplete, so scaffold correctly withheld the workflow).
                #
                # Cooled briefly rather than for the provider's rate-limit period:
                # nothing was consumed, so the key is fine — it is this connection
                # that was not.
                logger.warning(f"[{provider}] Transport error: {e!r}")
                api_key.mark_exhausted(TRANSPORT_COOLDOWN)
                last_error = e
                continue

        # Distinguish the ways we get here. Reporting "Last error: None" for the
        # cooldown case (nothing was tried, so nothing set last_error) made a
        # transient rate limit look like a hard crash.
        all_keys = [k for st in self._providers.values() for k in st.keys]

        if not all_keys:
            raise AllProvidersExhausted(
                "No LLM providers are configured — set GEMINI_API_KEY_1, GROQ_API_KEY_1 "
                "or ANTHROPIC_API_KEY_1 in backend/.env.",
                reason="not_configured",
            )

        # Only claim capacity is gone when EVERY key is out of credit or has burned
        # a daily quota. If even one is merely rate-limited, waiting fixes it and
        # saying otherwise would be a lie.
        if all(k.hard_blocked for k in all_keys):
            raise AllProvidersExhausted(
                "Every LLM provider is out of credit or has exhausted its quota. "
                "Test generation needs more capacity before it can run again.",
                reason="quota_exhausted",
            )

        if last_error is not None:
            raise AllProvidersExhausted(
                f"All LLM providers failed. Last error: {last_error}",
                reason="failed",
            )
        raise AllProvidersExhausted(
            f"Every LLM provider is rate-limited right now "
            f"({', '.join(cooling)}) — try again in a minute.",
            reason="rate_limited",
        )

    async def stream_complete(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.2,
        context_hint: str = "",
        json_mode: bool = False,
        tier: Tier = Tier.FREE,
    ) -> AsyncGenerator[str, None]:
        """
        Streaming version — yields text chunks as they arrive.
        Falls back to non-streaming if provider doesn't support it.
        """
        last_error = None
        emitted = 0                      # chunks forwarded to the caller, any provider

        for provider in PROVIDER_PRIORITY[tier]:
            if provider not in self._providers:
                continue

            async with self._lock:
                api_key = self._providers[provider].next_available_key()

            if api_key is None:
                continue

            spec = model_for(provider, tier)
            await self._broadcast_status(f"Streaming from {spec.id}...")

            t0 = time.perf_counter()
            provider_emitted = 0
            try:
                async for chunk in self._stream_provider(
                    provider, api_key, prompt, system_prompt, temperature, json_mode, spec
                ):
                    emitted += 1
                    provider_emitted += 1
                    yield chunk
                latency_ms = round((time.perf_counter() - t0) * 1000)
                api_key.record_success()
                self._log_call(provider, api_key, context_hint, success=True,
                               latency_ms=latency_ms, model=spec.id)
                record_provenance(ProviderCall(
                    provider=provider.value,
                    model=spec.id,
                    context=context_hint,
                    # Streaming providers don't report usage; None means
                    # "not reported", not zero.
                    total_tokens=None,
                ))
                return

            except RateLimitError:
                api_key.mark_exhausted(COOLDOWN[provider])
                last_error = f"{provider.value} rate limited"
                continue
            except ProviderError as e:
                api_key.mark_exhausted(COOLDOWN[provider] // 2)
                last_error = str(e)
                continue
            except Exception as e:
                # Transport-level failure: a TLS record-layer error, a dropped
                # connection, or a proxy/VPN that mangles long-lived streaming
                # sockets while leaving ordinary request/response calls intact.
                # The KEY itself is fine, so we deliberately do NOT cool it down
                # — doing so would starve the non-streaming fallback below of the
                # very key it needs. If we had already forwarded part of this
                # stream we can't cleanly restart it, so re-raise; otherwise move
                # on and let the fallback finish the job.
                api_key.error_count += 1
                last_error = f"{provider.value} stream transport error: {e}"
                logger.warning(
                    f"[{provider.value}] streaming failed ({e}); "
                    f"falling back to non-streaming"
                )
                if provider_emitted:
                    raise
                continue

        # Nothing streamed from any provider — e.g. every streaming socket was
        # killed at the TLS layer. Streaming is only a delivery detail, not the
        # result, so fall back to a single non-streaming completion and hand the
        # caller the whole text at once. This keeps generation working on
        # networks that break streaming but pass normal requests (proven: a
        # stream:true call fails "bad record mac" where stream:false returns 200).
        if emitted == 0:
            logger.info(
                "Streaming unavailable across all providers — "
                "falling back to a non-streaming completion"
            )
            text = await self.complete(
                prompt, system_prompt=system_prompt, temperature=temperature,
                context_hint=context_hint, json_mode=json_mode, tier=tier,
            )
            if text:
                yield text
                return

        raise AllProvidersExhausted(f"All providers failed streaming. Last: {last_error}")

    def provider_is_viable(self, provider: Provider) -> bool:
        """Whether this provider could realistically serve a request soon.

        Distinguishes the two ways a key can be unusable, because they mean
        opposite things to a user-facing claim:

          • hard_blocked — out of credit or a burned daily quota. Waiting does
            not fix it, so anything promising this provider is promising
            something we cannot deliver.
          • cooling down — a plain per-minute 429. Waiting *does* fix it, so the
            provider is still viable and a claim about it stays honest.

        Cold start reports viable: no key has failed yet, so we have no evidence
        it's blocked. The first real attempt corrects it.
        """
        state = self._providers.get(provider)
        if not state:
            return False
        return any(not k.hard_blocked for k in state.keys)

    def get_status(self) -> dict:
        """Return current health of all providers (for the UI status panel).

        "Available" means the same thing here as in get_key_health: not cooling
        AND not hard-blocked. It used to count only the cooldown timer, so a key
        that was out of credit — which no amount of waiting fixes — was reported
        as an available key on a healthy provider. The public status page then
        said "All systems operational" for an account that could not serve a
        single request, which is precisely the moment a status page has to be
        right.

        What this still cannot know is quota it has not yet been refused for:
        key state lives in memory, so after a restart every key reads healthy
        until something actually tries it. `never_used` lets the UI distinguish
        "confirmed working" from "not tried since boot" instead of implying the
        first when it only knows the second.
        """
        status = {}
        for provider, state in self._providers.items():
            available = sum(1 for k in state.keys if k.is_available and not k.hard_blocked)
            calls = sum(k.call_count for k in state.keys)
            status[provider.value] = {
                "total_keys": len(state.keys),
                "available_keys": available,
                "total_calls": calls,
                "healthy": available > 0,
                "hard_blocked_keys": sum(1 for k in state.keys if k.hard_blocked),
                # No successful call since this process started — the provider is
                # unproven, not proven good.
                "never_used": calls == 0,
            }
        return status

    def get_key_health(self) -> list[dict]:
        """Per-key health for the admin console.

        Identifies each key by the environment variable it came from, never by
        its value — not even a masked suffix. `GEMINI_API_KEY_2 is hard-blocked`
        already tells an operator exactly which line of .env to rotate, so
        putting any part of the secret on an HTTP response would buy nothing and
        risk it landing in a log, a screenshot, or a bug report.
        """
        now = time.time()
        health = []
        for provider, state in self._providers.items():
            for k in state.keys:
                health.append({
                    "provider": provider.value,
                    "env_var": f"{_ENV_PREFIXES[provider]}{k.index}",
                    "index": k.index,
                    "available": k.is_available and not k.hard_blocked,
                    # Distinct from a 429 cooldown: waiting does not fix these,
                    # so the console must not imply they'll heal on their own.
                    "hard_blocked": k.hard_blocked,
                    "cooldown_seconds_left": max(0, round(k.exhausted_until - now)),
                    "call_count": k.call_count,
                    "error_count": k.error_count,
                    "last_used": k.last_used or None,
                })
        return health

    def get_call_log(self, limit: int = 50) -> list[dict]:
        return self._call_log[-limit:]

    # ─────────────────────────────────────────
    # Provider-specific HTTP calls
    # ─────────────────────────────────────────

    async def _retry_transport(
        self,
        provider: Provider,
        api_key: APIKey,
        prompt: str,
        system_prompt: str,
        temperature: float,
        json_mode: bool,
        spec: ModelSpec,
    ) -> str:
        """Call a provider, retrying the SAME key on a transport failure.

        A TLS/connection error is a property of the socket, not of the key or the
        request: observed here as `SSL: RECORD_LAYER_FAILURE` hitting roughly half
        of connections to one provider, landing on a different key each time. The
        outer loop's response — cool this key, try the next provider — is the wrong
        move for that, and actively harmful when the other providers are dry: one
        unlucky socket then fails the whole call, which is how a generation died at
        the file-planning step with two healthy keys available.
        """
        for attempt in range(TRANSPORT_RETRIES + 1):
            try:
                return await self._call_provider(
                    provider, api_key, prompt, system_prompt, temperature, json_mode, spec
                )
            except (httpx.HTTPError, ssl.SSLError) as e:
                if attempt >= TRANSPORT_RETRIES:
                    raise
                logger.info(
                    f"[{provider}] transport error on key #{api_key.index} "
                    f"({e!r}) — reconnecting (attempt {attempt + 1}/{TRANSPORT_RETRIES})"
                )
                await asyncio.sleep(TRANSPORT_RETRY_DELAY * (attempt + 1))
        raise AssertionError("unreachable")     # pragma: no cover

    async def _call_provider(
        self,
        provider: Provider,
        api_key: APIKey,
        prompt: str,
        system_prompt: str,
        temperature: float,
        json_mode: bool,
        spec: ModelSpec,
    ) -> str:
        if provider == Provider.GEMINI:
            return await self._call_gemini(api_key, prompt, system_prompt, temperature, json_mode, spec)
        elif provider == Provider.GROQ:
            return await self._call_groq(api_key, prompt, system_prompt, temperature, json_mode, spec)
        elif provider == Provider.CLAUDE:
            return await self._call_claude(api_key, prompt, system_prompt, temperature, json_mode, spec)
        raise ValueError(f"Unknown provider: {provider}")

    async def _stream_provider(
        self,
        provider: Provider,
        api_key: APIKey,
        prompt: str,
        system_prompt: str,
        temperature: float,
        json_mode: bool,
        spec: ModelSpec,
    ) -> AsyncGenerator[str, None]:
        """Route to provider-specific streaming implementation."""
        if provider == Provider.GEMINI:
            async for chunk in self._stream_gemini(api_key, prompt, system_prompt, temperature, json_mode, spec):
                yield chunk
        elif provider == Provider.GROQ:
            async for chunk in self._stream_groq(api_key, prompt, system_prompt, temperature, json_mode, spec):
                yield chunk
        elif provider == Provider.CLAUDE:
            async for chunk in self._stream_claude(api_key, prompt, system_prompt, temperature, spec):
                yield chunk

    # ── Gemini ──────────────────────────────

    async def _call_with_user_key(
        self, user_key: str, prompt: str, system: str,
        temperature: float, json_mode: bool, tier: Tier, context_hint: str = "",
    ) -> str:
        """Run one Gemini call against a key the *user* supplied.

        Two properties matter here, and both are about not letting one user's key
        affect anyone else.

        The `APIKey` is constructed fresh and thrown away. It is never inserted
        into `self._providers`, so a user whose key is rate-limited or revoked
        cannot mark the operator's Gemini pool as cooling — which would have been
        a trivial way for one bad key to degrade generation for every other user
        on the instance.

        And failures propagate rather than rotating. `complete()` above decides
        what falling back means; this method's only job is "run it on their key,
        or say it didn't work".
        """
        spec = model_for(Provider.GEMINI, tier)
        # index=0 is cosmetic — it only appears in log lines, and this key is not
        # in any pool to have a position in.
        key = APIKey(key=user_key, provider=Provider.GEMINI, index=0)
        await self._broadcast_status(f"Using {spec.id} (your key)...")

        text = await self._call_gemini(key, prompt, system, temperature, json_mode, spec)
        # Recorded so the provenance report names the model that actually
        # answered, exactly as it does for a shared-pool call. The report says
        # which model ran; it does not say whose key paid, and it should not —
        # that is between the user and their provider.
        record_provenance(ProviderCall(
            provider=Provider.GEMINI.value,
            model=spec.id,
            context=context_hint,
            total_tokens=(_last_usage.get() or {}).get("total"),
        ))
        return text

    async def _call_gemini(self, key: APIKey, prompt, system, temperature, json_mode: bool, spec: ModelSpec) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{spec.id}:generateContent?key={key.key}"
        )
        gen_config = {
            "temperature": temperature,
            "maxOutputTokens": spec.max_output_tokens,
        }
        if json_mode:
            gen_config["responseMimeType"] = "application/json"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": system}]} if system else None,
            "generationConfig": gen_config,
        }
        # Remove None fields
        body = {k: v for k, v in body.items() if v is not None}

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=body)

        if r.status_code == 429:
            raise RateLimitError(f"Gemini rate limit: {r.text}")
        if r.status_code != 200:
            raise ProviderError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")

        data = r.json()
        um = data.get("usageMetadata") or {}
        _last_usage.set({
            "prompt": um.get("promptTokenCount"),
            "completion": um.get("candidatesTokenCount"),
            "total": um.get("totalTokenCount"),
        })
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"Unexpected Gemini response shape: {e}")

    async def _stream_gemini(self, key: APIKey, prompt, system, temperature, json_mode: bool, spec: ModelSpec):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{spec.id}:streamGenerateContent?alt=sse&key={key.key}"
        )
        gen_config = {"temperature": temperature}
        if json_mode:
            gen_config["responseMimeType"] = "application/json"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", url, json=body) as r:
                if r.status_code == 429:
                    raise RateLimitError("Gemini rate limited")
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        import json
                        try:
                            chunk = json.loads(line[6:])
                            text = chunk["candidates"][0]["content"]["parts"][0]["text"]
                            yield text
                        except Exception:
                            continue

    # ── Groq ────────────────────────────────

    async def _call_groq(self, key: APIKey, prompt, system, temperature, json_mode: bool, spec: ModelSpec) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": spec.id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": spec.max_output_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {key.key}"},
            )

        if r.status_code == 429:
            raise RateLimitError(f"Groq rate limit: {r.text}")
        if r.status_code != 200:
            raise ProviderError(f"Groq HTTP {r.status_code}: {r.text[:200]}")

        data = r.json()
        _last_usage.set(_openai_usage(data.get("usage")))
        return data["choices"][0]["message"]["content"]

    async def _stream_groq(self, key: APIKey, prompt, system, temperature, json_mode: bool, spec: ModelSpec):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": spec.id,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "max_tokens": spec.max_output_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST",
                "https://api.groq.com/openai/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {key.key}"},
            ) as r:
                if r.status_code == 429:
                    raise RateLimitError("Groq rate limited")
                async for line in r.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        import json
                        try:
                            chunk = json.loads(line[6:])
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield delta
                        except Exception:
                            continue

    # ── Claude ──────────────────────────────

    def _claude_body(self, prompt: str, system: str, temperature: float, spec: ModelSpec) -> dict:
        """Build an Anthropic request for whichever model this tier resolved to.

        The two tiers do not share a request shape. Haiku takes `temperature`;
        Opus 4.7+ removed the sampling parameters and returns a 400 if one is
        sent, so `temperature` is gated on the spec rather than always included.
        Thinking is likewise opt-in per model: it is off unless asked for on
        Opus 4.8, and `budget_tokens` is gone — depth comes from `effort`.
        """
        body: dict = {
            "model": spec.id,
            "max_tokens": spec.max_output_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if spec.accepts_temperature:
            body["temperature"] = temperature
        if spec.adaptive_thinking:
            body["thinking"] = {"type": "adaptive"}
        if spec.effort:
            body["output_config"] = {"effort": spec.effort}
        if system:
            body["system"] = system
        return body

    async def _call_claude(self, key: APIKey, prompt, system, temperature, json_mode: bool, spec: ModelSpec) -> str:
        # Anthropic has no response_format flag; it follows JSON instructions in
        # the prompt reliably, so json_mode is accepted for a uniform interface
        # but needs no request change here.
        body = self._claude_body(prompt, system, temperature, spec)

        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                json=body,
                headers={
                    "x-api-key": key.key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )

        if r.status_code == 429:
            raise RateLimitError(f"Claude rate limit: {r.text}")
        if r.status_code != 200:
            raise ProviderError(f"Claude HTTP {r.status_code}: {r.text[:200]}")

        data = r.json()
        cu = data.get("usage") or {}
        _last_usage.set({
            "prompt": cu.get("input_tokens"),
            "completion": cu.get("output_tokens"),
            "total": (cu.get("input_tokens") or 0) + (cu.get("output_tokens") or 0) or None,
        })

        # Concatenate the text blocks rather than reading content[0]. With
        # adaptive thinking on, content[0] is a thinking block and indexing it
        # for "text" raises KeyError — every Pro call would fail while looking
        # like a malformed-response bug.
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        if not text:
            raise ProviderError(
                f"Claude returned no text block (stop_reason={data.get('stop_reason')})"
            )
        return text

    async def _stream_claude(self, key: APIKey, prompt, system, temperature, spec: ModelSpec):
        body = self._claude_body(prompt, system, temperature, spec)
        body["stream"] = True

        async with httpx.AsyncClient(timeout=90) as client:
            async with client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                json=body,
                headers={
                    "x-api-key": key.key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            ) as r:
                if r.status_code == 429:
                    raise RateLimitError("Claude rate limited")
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        import json
                        try:
                            event = json.loads(line[6:])
                            if event.get("type") == "content_block_delta":
                                delta = event.get("delta") or {}
                                # Only text_delta is the answer. With adaptive
                                # thinking on, thinking_delta arrives on the same
                                # event type and must not be streamed to the user
                                # as if it were generated code.
                                if delta.get("type") == "text_delta":
                                    yield delta.get("text", "")
                        except Exception:
                            continue

    # ─────────────────────────────────────────
    # Logging & SSE status broadcasts
    # ─────────────────────────────────────────

    def _log_call(self, provider, key, context, success, latency_ms=None, usage=None, model=None):
        self._call_log.append({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "provider": provider.value,
            "model": model,
            "key_index": key.index,
            "context": context,
            "success": success,
            "latency_ms": latency_ms,
            "prompt_tokens": (usage or {}).get("prompt"),
            "completion_tokens": (usage or {}).get("completion"),
            "total_tokens": (usage or {}).get("total"),
        })
        # Keep log at a reasonable size
        if len(self._call_log) > 1000:
            self._call_log = self._call_log[-500:]

    async def _broadcast_status(self, message: str):
        """Send a status update to the listener scoped to the current request."""
        callback = _status_listener.get()
        if callback is None:
            return
        try:
            await callback(message)
        except Exception:
            pass

    def subscribe_status(self, callback):
        """Register a status callback for the current async task only."""
        _status_listener.set(callback)

    def unsubscribe_status(self, callback=None):
        """Clear the current task's status callback (arg kept for compatibility)."""
        _status_listener.set(None)


# ─────────────────────────────────────────────
# Custom exceptions
# ─────────────────────────────────────────────

async def probe_user_key(api_key: str) -> tuple[bool, str]:
    """Is this Gemini key usable right now? Returns (ok, reason_if_not).

    Called when a user saves a key, so the answer arrives in the form they are
    looking at rather than in the middle of a generation minutes later. A
    well-formed key can still be revoked, billing-disabled, or scoped to a
    project without the Generative Language API enabled, and none of that is
    visible from the string.

    The probe is the cheapest real call available — a handful of tokens — because
    the only question is whether the credential is accepted at all. It never
    touches the shared pool.
    """
    key = APIKey(key=api_key, provider=Provider.GEMINI, index=0)
    spec = model_for(Provider.GEMINI, Tier.FREE)
    try:
        await router._call_gemini(
            key, prompt="Reply with the single word: ok",
            system="", temperature=0.0, json_mode=False, spec=spec,
        )
        return True, ""
    except RateLimitError:
        # The key is real — it is being throttled, which is a fact about right
        # now rather than about the key. Rejecting it here would be wrong.
        return True, ""
    except ProviderError as e:
        detail = str(e)
        low = detail.lower()
        if "api key not valid" in low or "api_key_invalid" in low or "401" in low or "403" in low:
            return False, "Google rejected it. Check the key, and that the Generative Language API is enabled for its project."
        return False, detail[:200]
    except Exception as e:                      # transport, DNS, TLS
        return False, f"Couldn't reach Google to check it ({type(e).__name__}). Try again shortly."


class RateLimitError(Exception):
    """Provider returned 429 or signalled quota exhaustion."""

class ProviderError(Exception):
    """Non-rate-limit error from a provider (HTTP 5xx, malformed response, etc.)."""

class AllProvidersExhausted(Exception):
    """Every provider and every key has been tried and failed.

    `reason` lets callers tell a transient rate limit ("rate_limited") apart
    from genuinely spent capacity ("quota_exhausted") — only the latter should
    ever be surfaced to a user as an upgrade prompt.
    """

    def __init__(self, message: str, reason: str = "failed"):
        super().__init__(message)
        self.reason = reason


# ─────────────────────────────────────────────
# Singleton instance (imported by agents)
# ─────────────────────────────────────────────

router = LLMRouter()
