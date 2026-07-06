# Testra — Architecture

This document describes how Testra is put together: the request flow, the
two-agent pipeline, the multi-provider LLM router, and the persistence and
observability layers.

## High-level flow

```
┌────────────┐    POST /api/analyze     ┌──────────────────────────────────────┐
│  Frontend  │ ───────────────────────▶ │            FastAPI (main.py)         │
│ React/Vite │ ◀─────────────────────── │  request-id mw · CORS · /metrics     │
└────────────┘   job_id + analysis      └───────────────┬──────────────────────┘
      │                                                  │
      │  GET /api/stream/{job_id}  (SSE)                 ▼
      │                                   ┌──────────────────────────────┐
      │                                   │  Agent 1: FilterAgent        │
      │                                   │   • FileExtractor (local)    │
      │                                   │   • LLM project analysis     │
      │                                   └──────────────┬───────────────┘
      │                                                  │ FilterResult
      │  POST /api/generate/{job_id}                     ▼
      │                                   ┌──────────────────────────────┐
      └─────────────────────────────────▶│  Agent 2: WriterAgent        │
                                          │   • selector grounding       │
                                          │   • test generation (JSON)   │
                                          │   • selector validation      │
                                          └──────────────┬───────────────┘
                                                         │
                        ┌────────────────────────────────┴───────────┐
                        ▼                                             ▼
              ┌───────────────────┐                       ┌────────────────────┐
              │   LLMRouter       │  Gemini→Groq→Claude→   │   JobStore (SQLite)│
              │  key rotation +   │  Together, per-key     │  durable sessions  │
              │  failover + JSON  │  cooldown on 429       │  survive restarts  │
              └───────────────────┘                       └────────────────────┘
```

## Components

### `main.py` — API layer
FastAPI app exposing `/api/analyze`, `/api/upload-zip`, `/api/generate/{job_id}`,
`/api/stream/{job_id}` (SSE), `/api/status`, `/api/logs`, `/health`, `/metrics`.
Adds a per-request correlation ID (`X-Request-ID`) and CORS from config.

### `services/file_extractor.py` — deterministic pre-filter
Cheap, local, no-LLM work: filters a repo down to browser-facing UI files,
scores them 1–10 by importance, fits them into a token budget (summarizing
low-value files instead of dropping them), and detects the tech stack
(React/Vue/Next/Nuxt/Angular/Svelte **and** Django/Flask/FastAPI/Rails/Laravel
+ template engines).

### `agents/filter_agent.py` — Agent 1
Runs the extractor, then asks the LLM (in JSON mode) for a structured project
analysis: summary, key pages, routes, and testing challenges.

### `agents/writer_agent.py` — Agent 2
Builds a grounding list of **real selectors** (ids, data-testid/data-cy, names,
classes) from the source and injects it into the prompt, generates the suite in
JSON mode (POM classes + specs + config), appends CI YAML + README, then
validates every generated selector against the source and flags hallucinations.

### `services/llm_router.py` — provider rotation engine
Tries providers in priority order (Gemini → Groq → Claude → Together), rotates
round-robin through each provider's keys, and puts a key on a cooldown when it
returns 429/errors. A `json_mode` flag forces valid-JSON output per provider.
Per-request status updates are scoped with a `ContextVar` so concurrent streams
don't cross-talk.

### `services/store.py` — durable job store
SQLite-backed session store (stdlib only). Jobs survive restarts and are shared
across workers; old jobs are pruned by TTL. `FilterResult` is serialized to/from
JSON transparently.

### `config.py` / `logging_config.py`
Typed settings via `pydantic-settings`; structured JSON logging with request IDs.

## Request lifecycle
1. **Analyze** — fetch repo (GitHub API/raw or ZIP) → `FileExtractor` → Agent 1 →
   persist a job in SQLite → return `job_id` + analysis.
2. **Stream** (optional) — SSE streams Agent 2's output token-by-token; the raw
   output is cached on the job so generate can reuse it (no double LLM call).
3. **Generate** — load the job, run Agent 2 (or reuse streamed output), validate
   selectors, return the file set.

## Testing
`backend/tests/` covers the extractor + scoring, framework detection, router
failover/rotation, writer-agent logic (selector extraction/validation, JSON
parsing, framework normalization, test counting), URL parsing, and the store.
Run with `pytest tests/ -v`. CI (`.github/workflows/ci.yml`) runs the backend
matrix (Py 3.11–3.13) and a frontend build on every push.
