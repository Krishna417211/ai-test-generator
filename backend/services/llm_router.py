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


@dataclass
class APIKey:
    key: str
    provider: Provider
    index: int                          # position in the key pool
    call_count: int = 0
    error_count: int = 0
    exhausted_until: float = 0.0       # epoch seconds — 0 means available
    last_used: float = 0.0

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

PROVIDER_PRIORITY = [
    Provider.GEMINI,    # 1M context — best for large repos
    Provider.GROQ,      # Fastest inference
    Provider.CLAUDE,    # Reliable fallback
]

# How long to wait before retrying an exhausted key (seconds)
COOLDOWN = {
    Provider.GEMINI:   60,
    Provider.GROQ:     30,
    Provider.CLAUDE:   120,
}

# Max tokens we'll request from each provider
MAX_OUTPUT_TOKENS = {
    Provider.GEMINI:   8192,
    Provider.GROQ:     4096,
    Provider.CLAUDE:   4096,
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
        key_patterns = {
            Provider.GEMINI:   "GEMINI_API_KEY_",
            Provider.GROQ:     "GROQ_API_KEY_",
            Provider.CLAUDE:   "ANTHROPIC_API_KEY_",
        }

        for provider, prefix in key_patterns.items():
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
    ) -> str:
        """
        Try each provider in priority order until one succeeds.
        Returns the complete LLM response as a string.
        """
        last_error = None

        for provider in PROVIDER_PRIORITY:
            if provider not in self._providers:
                continue

            state = self._providers[provider]

            async with self._lock:
                api_key = state.next_available_key()

            if api_key is None:
                logger.info(f"[{provider}] All keys exhausted — skipping")
                continue

            await self._broadcast_status(f"Using {provider.value}...")

            _last_usage.set(None)
            t0 = time.perf_counter()
            try:
                result = await self._call_provider(
                    provider, api_key, prompt, system_prompt, temperature, json_mode
                )
                latency_ms = round((time.perf_counter() - t0) * 1000)
                api_key.record_success()
                self._log_call(provider, api_key, context_hint, success=True,
                               latency_ms=latency_ms, usage=_last_usage.get())
                return result

            except RateLimitError as e:
                logger.warning(f"[{provider}] Rate limited: {e}")
                api_key.mark_exhausted(COOLDOWN[provider])
                last_error = e
                continue

            except ProviderError as e:
                logger.error(f"[{provider}] Error: {e}")
                api_key.mark_exhausted(COOLDOWN[provider] // 2)
                last_error = e
                continue

        raise AllProvidersExhausted(
            f"All LLM providers failed. Last error: {last_error}"
        )

    async def stream_complete(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.2,
        context_hint: str = "",
        json_mode: bool = False,
    ) -> AsyncGenerator[str, None]:
        """
        Streaming version — yields text chunks as they arrive.
        Falls back to non-streaming if provider doesn't support it.
        """
        last_error = None

        for provider in PROVIDER_PRIORITY:
            if provider not in self._providers:
                continue

            async with self._lock:
                api_key = self._providers[provider].next_available_key()

            if api_key is None:
                continue

            await self._broadcast_status(f"Streaming from {provider.value}...")

            t0 = time.perf_counter()
            try:
                async for chunk in self._stream_provider(
                    provider, api_key, prompt, system_prompt, temperature, json_mode
                ):
                    yield chunk
                latency_ms = round((time.perf_counter() - t0) * 1000)
                api_key.record_success()
                self._log_call(provider, api_key, context_hint, success=True,
                               latency_ms=latency_ms)
                return

            except RateLimitError:
                api_key.mark_exhausted(COOLDOWN[provider])
                last_error = f"{provider.value} rate limited"
                continue
            except ProviderError as e:
                api_key.mark_exhausted(COOLDOWN[provider] // 2)
                last_error = str(e)
                continue

        raise AllProvidersExhausted(f"All providers failed streaming. Last: {last_error}")

    def get_status(self) -> dict:
        """Return current health of all providers (for the UI status panel)."""
        status = {}
        for provider, state in self._providers.items():
            available = sum(1 for k in state.keys if k.is_available)
            status[provider.value] = {
                "total_keys": len(state.keys),
                "available_keys": available,
                "total_calls": sum(k.call_count for k in state.keys),
                "healthy": available > 0,
            }
        return status

    def get_call_log(self, limit: int = 50) -> list[dict]:
        return self._call_log[-limit:]

    # ─────────────────────────────────────────
    # Provider-specific HTTP calls
    # ─────────────────────────────────────────

    async def _call_provider(
        self,
        provider: Provider,
        api_key: APIKey,
        prompt: str,
        system_prompt: str,
        temperature: float,
        json_mode: bool = False,
    ) -> str:
        if provider == Provider.GEMINI:
            return await self._call_gemini(api_key, prompt, system_prompt, temperature, json_mode)
        elif provider == Provider.GROQ:
            return await self._call_groq(api_key, prompt, system_prompt, temperature, json_mode)
        elif provider == Provider.CLAUDE:
            return await self._call_claude(api_key, prompt, system_prompt, temperature, json_mode)
        raise ValueError(f"Unknown provider: {provider}")

    async def _stream_provider(
        self,
        provider: Provider,
        api_key: APIKey,
        prompt: str,
        system_prompt: str,
        temperature: float,
        json_mode: bool = False,
    ) -> AsyncGenerator[str, None]:
        """Route to provider-specific streaming implementation."""
        if provider == Provider.GEMINI:
            async for chunk in self._stream_gemini(api_key, prompt, system_prompt, temperature, json_mode):
                yield chunk
        elif provider == Provider.GROQ:
            async for chunk in self._stream_groq(api_key, prompt, system_prompt, temperature, json_mode):
                yield chunk
        elif provider == Provider.CLAUDE:
            async for chunk in self._stream_claude(api_key, prompt, system_prompt, temperature):
                yield chunk

    # ── Gemini ──────────────────────────────

    async def _call_gemini(self, key: APIKey, prompt, system, temperature, json_mode: bool = False) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={key.key}"
        )
        gen_config = {
            "temperature": temperature,
            "maxOutputTokens": MAX_OUTPUT_TOKENS[Provider.GEMINI],
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

    async def _stream_gemini(self, key: APIKey, prompt, system, temperature, json_mode: bool = False):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:streamGenerateContent?alt=sse&key={key.key}"
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

    async def _call_groq(self, key: APIKey, prompt, system, temperature, json_mode: bool = False) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": "llama-3.3-70b-versatile",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": MAX_OUTPUT_TOKENS[Provider.GROQ],
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

    async def _stream_groq(self, key: APIKey, prompt, system, temperature, json_mode: bool = False):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": "llama-3.3-70b-versatile",
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "max_tokens": MAX_OUTPUT_TOKENS[Provider.GROQ],
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

    async def _call_claude(self, key: APIKey, prompt, system, temperature, json_mode: bool = False) -> str:
        # Anthropic has no response_format flag; it follows JSON instructions in
        # the prompt reliably, so json_mode is accepted for a uniform interface
        # but needs no request change here.
        body = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": MAX_OUTPUT_TOKENS[Provider.CLAUDE],
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system

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
        return data["content"][0]["text"]

    async def _stream_claude(self, key: APIKey, prompt, system, temperature):
        body = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": MAX_OUTPUT_TOKENS[Provider.CLAUDE],
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        if system:
            body["system"] = system

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
                                yield event["delta"].get("text", "")
                        except Exception:
                            continue

    # ─────────────────────────────────────────
    # Logging & SSE status broadcasts
    # ─────────────────────────────────────────

    def _log_call(self, provider, key, context, success, latency_ms=None, usage=None):
        self._call_log.append({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "provider": provider.value,
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

class RateLimitError(Exception):
    """Provider returned 429 or signalled quota exhaustion."""

class ProviderError(Exception):
    """Non-rate-limit error from a provider (HTTP 5xx, malformed response, etc.)."""

class AllProvidersExhausted(Exception):
    """Every provider and every key has been tried and failed."""


# ─────────────────────────────────────────────
# Singleton instance (imported by agents)
# ─────────────────────────────────────────────

router = LLMRouter()
