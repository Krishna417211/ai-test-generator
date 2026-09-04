#!/usr/bin/env python3
"""Verify every model in MODELS is still served by its provider.

Run this on a schedule. It exists because of a failure the test suite is
structurally unable to catch: on 2026-09-04 Groq retired the whole Llama 3.x
chat line, `llama-3.3-70b-versatile` stopped existing, and every Groq call in
production began failing — while all 1096 tests stayed green, because they mock
the providers and none of them has ever made a real call.

The failure mode is quiet by construction. The key authenticates, the request is
well-formed, and only the model NAME is rejected, so the provider looks alive
right up to the point every call fails. Nothing surfaces until a user notices
generation is broken.

What it checks, per provider, using a key already configured:

  1. the key authenticates at all
  2. every model id MODELS names for that provider appears in the provider's
     own model listing

Gemini availability is per-Google-project, so EVERY Gemini key is checked
separately rather than just the first: a model can be served to one project and
404 for another, which strands the keys it isn't served to while the others keep
working — exactly the case the MODELS comment warns about.

Exit codes:  0 everything available   1 something missing   2 nothing to check

    python backend/scripts/check_models.py            # human-readable
    python backend/scripts/check_models.py --quiet    # only problems

On the deployed box, where the keys live:

    cd /opt/testra && sudo docker compose -f deploy/docker-compose.prod.yml \
        exec -T backend python scripts/check_models.py
"""
from __future__ import annotations

import argparse
import os
import sys

# Importable whether run as `python backend/scripts/check_models.py` from the
# repo root or as `python scripts/check_models.py` from the container's /app.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

# _ENV_PREFIXES is private to the router, and imported anyway on purpose: the
# point of this script is to check the SAME table the app runs on, so reading
# the app's own definitions is the feature. A local copy would drift, which is
# the exact class of bug this exists to catch.
from services.llm_router import (  # noqa: E402
    MODELS, Provider, _ENV_PREFIXES as ENV_PREFIX,
)

TIMEOUT = 30
# Some provider edges reject the default client UA outright (Groq answers a
# bare urllib with Cloudflare 1010), so identify ourselves properly.
HEADERS = {"User-Agent": "testra-model-check/1.0"}


def keys_for(provider: Provider) -> list[tuple[str, str]]:
    """Every configured key for a provider, as (env var name, value)."""
    prefix = ENV_PREFIX[provider]
    out = []
    for i in range(1, 11):
        name = f"{prefix}{i}"
        val = os.environ.get(name, "").strip()
        if val:
            out.append((name, val))
    return out


def available_gemini(key: str) -> set[str]:
    r = httpx.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": key, "pageSize": 1000}, headers=HEADERS, timeout=TIMEOUT,
    )
    r.raise_for_status()
    return {m["name"].removeprefix("models/") for m in r.json().get("models", [])}


def available_groq(key: str) -> set[str]:
    r = httpx.get(
        "https://api.groq.com/openai/v1/models",
        headers={**HEADERS, "Authorization": f"Bearer {key}"}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    return {m["id"] for m in r.json().get("data", [])}


def available_claude(key: str) -> set[str]:
    r = httpx.get(
        "https://api.anthropic.com/v1/models",
        headers={**HEADERS, "x-api-key": key, "anthropic-version": "2023-06-01"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return {m["id"] for m in r.json().get("data", [])}


LISTERS = {
    Provider.GEMINI: available_gemini,
    Provider.GROQ: available_groq,
    Provider.CLAUDE: available_claude,
}


def wanted() -> dict[Provider, set[str]]:
    """Every model id MODELS names, grouped by provider."""
    out: dict[Provider, set[str]] = {}
    for (provider, _tier), spec in MODELS.items():
        out.setdefault(provider, set()).add(spec.id)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="print only problems")
    args = ap.parse_args()

    say = (lambda *a: None) if args.quiet else print
    problems: list[str] = []
    checked = 0

    for provider, ids in sorted(wanted().items(), key=lambda kv: kv[0].value):
        keys = keys_for(provider)
        if not keys:
            say(f"\n{provider.value}: no keys configured — skipped")
            continue

        say(f"\n{provider.value}: expecting {', '.join(sorted(ids))}")
        # Gemini availability is per-project, so every key is asked. The others
        # are account-wide, but asking each key costs one cheap GET and catches
        # a single revoked key in a rotation, which is worth the same call.
        for name, key in keys:
            checked += 1
            try:
                have = LISTERS[provider](key)
            except httpx.HTTPStatusError as e:
                msg = f"{provider.value}/{name}: cannot list models (HTTP {e.response.status_code})"
                problems.append(msg)
                say(f"  [{name}] AUTH FAIL — HTTP {e.response.status_code}")
                continue
            except Exception as e:                       # network, DNS, TLS
                msg = f"{provider.value}/{name}: {type(e).__name__} listing models"
                problems.append(msg)
                say(f"  [{name}] ERROR — {type(e).__name__}")
                continue

            missing = sorted(ids - have)
            if missing:
                for m in missing:
                    problems.append(f"{provider.value}/{name}: '{m}' is no longer served")
                say(f"  [{name}] MISSING: {', '.join(missing)}")
                near = sorted(h for h in have if not h.startswith("whisper"))[:10]
                say(f"           this key can use: {', '.join(near) or '(none)'}")
            else:
                say(f"  [{name}] ok — all present ({len(have)} models offered)")

    if not checked:
        print("No provider keys configured — nothing to check.", file=sys.stderr)
        return 2

    if problems:
        # Detail goes to stdout, the verdict to stderr; flush first so a
        # piped run cannot print the summary before the findings it
        # summarises.
        sys.stdout.flush()
        print(f"\nFAIL — {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nA model listed in MODELS but not served by the provider fails EVERY "
            "call to it. Update backend/services/llm_router.py to an id the "
            "provider still lists.",
            file=sys.stderr,
        )
        return 1

    say(f"\nOK — every model in MODELS is available on all {checked} key(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
