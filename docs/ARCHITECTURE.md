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
              │   LLMRouter       │  Gemini→Groq→Claude    │   JobStore (SQLite)│
              │  key rotation +   │  per-key cooldown      │  durable sessions  │
              │  failover + JSON  │  on 429                │  survive restarts  │
              └───────────────────┘                       └────────────────────┘
```

## Components

### `main.py` — API layer
FastAPI app exposing `/api/analyze`, `/api/upload-zip`, `/api/generate/{job_id}`,
`/api/stream/{job_id}` (SSE), `/api/status`, `/api/logs`, `/health`, `/metrics`.
Adds a per-request correlation ID (`X-Request-ID`) and CORS from config.

### `services/progress.py` — step progress for the long POSTs
`/api/analyze`, `/api/upload-zip`, `/api/scan` and `/api/publish-zip` stream
NDJSON: one `{"type":"step"}` line per phase as it starts and finishes, then a
single `result` or `error`. The client (`FlowPipeline`) lights up a flowchart
from it, so the 10–60s wait shows which phase is running and what it found —
`{"id":"extract","state":"done","detail":"18 of 240 files kept · React"}`.

NDJSON over the endpoint's own body rather than SSE because all four are POSTs
and two carry a multipart upload, while EventSource can only GET with no headers
and no body. This way one request keeps its auth header and its file.

The trade-off it forces: the status line is sent before the work runs, so a
mid-flight failure can't *be* an HTTP status. Errors therefore split by when
they're decidable — anything pre-flight (a malformed URL, a missing token, a
spent quota) is raised **before** the first byte and stays a real 400/403/402;
anything after is emitted in-band carrying its status, and the client re-raises
the identical exception (`raiseApiError` in `utils/api.ts`) so a 402 opens the
upgrade modal either way.

Step ids are a cross-language contract with `frontend/src/flows.ts`. A drifted
id is silent in the browser — the card simply never lights — so `Progress`
rejects an unknown id rather than emitting an event nothing listens for.

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
classes) from the source and injects it into the prompt, plans the file list,
generates each file in its own call (so a large suite can't truncate), then
validates every generated selector against the source and flags hallucinations.

The model writes page objects and specs — nothing else. Everything that makes
them *runnable* comes from `agents/scaffold.py`.

### `agents/scaffold.py` — the runnable skeleton
The dependency manifest, framework config, CI pipelines and README: boilerplate
with one correct answer per framework, so it is templated rather than asked of
an LLM.

This is a separate module because the alternative failed silently for every
user. The CI template used to run `npm ci` while nothing ever emitted a
`package.json` — so the pipeline could not pass on any repo, and neither half
knew about the other. The install step and the manifest it installs are now
written by the same module.

Two invariants it holds:

* **The suite owns a directory (`e2e/`), not the repo root.** A React app already
  has a `package.json` there; writing ours over it would destroy the manifest of
  the app under test.
* **`BASE_URL` wins, then a local `webServer`.** One variable points the same
  suite at staging or prod; unset, the config boots the app so a fresh clone and
  CI both work with no arguments. Base URLs are normalized to a trailing slash
  and tests navigate with slashless relative paths — the only combination under
  which a sub-path deployment (a GitHub Pages project site) stays addressable.

### `services/llm_router.py` — provider rotation engine
Tries providers in priority order (Gemini → Groq → Claude), rotates
round-robin through each provider's keys, and puts a key on a cooldown when it
returns 429/errors. A `json_mode` flag forces valid-JSON output per provider.
Per-request status updates are scoped with a `ContextVar` so concurrent streams
don't cross-talk.

### `services/store.py` — durable job store
SQLite-backed session store (stdlib only). Jobs survive restarts and are shared
across workers; old jobs are pruned by TTL. `FilterResult` is serialized to/from
JSON transparently.

### `services/admin.py` — the admin role
`require_admin` gates every `/api/admin/*` route: user management, plan and quota
overrides, platform analytics, and provider/key health.

The grant lives in `ADMIN_EMAILS` (config), **not** in a `users.role` column, and
that is the design rather than an omission. With no row that confers privilege
there is nothing to write to escalate, no promote-user endpoint to abuse, and no
path from a stolen session to a second admin. The trade is that changing the
admin list is a config change plus a restart — the right friction for the one
role that can read every account.

Two rules the module exists to enforce:

* **The address must be verified.** With `REQUIRE_EMAIL_VERIFICATION=false` a
  stranger can sign up as an admin's address and still be issued a session, so
  the allowlist alone is not proof of who holds the address (`auth.is_admin`).
* **Non-admins get 404, not 403.** A 403 confirms the console exists to someone
  who already has a session; to everyone else it should look absent.

The client's `user.is_admin` decides whether the nav item renders and nothing
else — every route re-derives the role server-side.

### `config.py` / `logging_config.py`
Typed settings via `pydantic-settings`; structured JSON logging with request IDs.

## Request lifecycle
1. **Analyze** — fetch repo (GitHub API/raw or ZIP) → `FileExtractor` → Agent 1 →
   persist a job in SQLite → return `job_id` + analysis. Streams a step per phase
   as it happens (`services/progress.py`).
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
