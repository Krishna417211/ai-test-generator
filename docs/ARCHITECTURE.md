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
them *runnable* comes from `agents/scaffold.py`, and everything that makes them
*trustworthy* comes from the grounding/fragility pipeline below.

### The trust pipeline — grounding, self-heal, fragility

The failure mode this product exists to prevent is a suite that looks perfect
and breaks on the first run because the model invented a selector. Four
components attack that, and none of them execute the generated code (there is no
sandbox — see `services/validator.py`); they reason about the code and the app
statically.

#### `services/grounding.py` — does every selector resolve to something real?
Extracts the anchors that genuinely exist (ids, test-ids, classes, form names,
ARIA roles, accessible labels, link/button text) from two ground truths and
checks each generated selector against them:

* **source grounding** — the repo's markup, carrying *provenance*: the file and
  line each anchor was declared on, so the UI can cite `LoginForm.tsx:42` instead
  of asking for trust. Works for any repo.
* **DOM grounding** — the deployed page's live HTML, fetched through the security
  scanner's SSRF-guarded, non-executing path (`validate_target` + `_redirect_guard`).
  Only when the user supplies a URL they own; it is the stronger evidence because
  it is what the browser will actually see.

Both are built identically (extract anchor set → membership test), so a selector
verified against source and one verified against the live DOM are judged by the
same rules. No CSS engine is needed: the selectors we emit reduce to a handful of
anchor kinds, and membership *is* the question "would `querySelector` find this".

The **self-heal loop** in the writer agent consumes the unverified set: it
re-prompts the model with the exact selectors that missed and the real anchors
that exist, so the fix is a grounded substitution, not another guess.

#### `services/fragility.py` — which selectors will break *later*?
A selector can be correct today and brittle tomorrow (`div:nth-child(3) > button`).
A weighted feature model (a linear scorer over named, inspectable features —
positional depth, hashed classes, absolute XPath, versus stable contracts like
`data-testid`/role) predicts break-risk 0→1 and grades the suite A–F. The weights
are *data* (`FEATURE_WEIGHTS`), not code, so the benchmark harness can re-fit them.

#### `services/importance_model.py` — a learned file-importance scorer
`file_extractor.score_file` ranks files with frozen path heuristics. This is a
from-scratch logistic regression (no numpy/sklearn — the math is in the module)
that refines the score using the file's body, learning per-codebase which files
are worth testing. It degrades safely: with no trained `importance_model.json` on
disk, callers fall back to the heuristic, so it is a no-op until trained. Labels
bootstrap from weak supervision and are meant to be re-fit on real grounding
outcomes.

#### `services/benchmark.py` — the numbers, over many repos
Runs grounding + fragility across a set of (repo, suite) cases and reports the
verified-selector rate (pooled and per-repo) and fragility-grade distribution as
a markdown leaderboard. Because we never execute, the honest headline metric is
"selectors provably resolve", not an invented pass rate. `python -m services.benchmark cases.json`.

All of this surfaces in the frontend `TrustPanel`: the live-DOM badge, the
per-selector provenance, and the durability grade — each a real measurement,
never dressed up as a prediction that the suite passes.

### `services/live_crawler.py` — what the suite is actually written from

The crawl-first flow grounds generation on the deployed site, so this module's
reach *is* the product's ceiling: anything it cannot render, the suite cannot
test. Three things it does beyond fetching pages, each because the naive version
produced a suite that tested a login form and invented the rest:

* **Routes come from the app's JavaScript, not just `a[href]`.** The entry point
  of most apps worth testing is a sign-in screen that links nowhere and navigates
  in script (`location.replace('products.html')`). Route-shaped literals in
  inline and same-origin scripts are followed too.
* **It can sign in first** (`LoginSpec`). A guarded app bounces every route back
  to its login page, so an anonymous crawl of one indexes a single screen no
  matter how many routes exist. A requested sign-in that doesn't take is a hard
  failure (`CrawlLoginFailed`), never a silent downgrade — the caller gave
  credentials precisely because the content is behind them. Credentials are
  per-request: dropped before the job is persisted, and kept out of logs.
  Logout links are never followed, or the crawl would end its own session.
* **It counts what it really rendered.** Redirects are resolved to their final
  URL before indexing, so a login wall is one page rather than one per guarded
  route, and near-identical instances of a template (`product.html?id=1..500`)
  are capped so the page budget reaches distinct routes like checkout.

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
