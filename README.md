# Testra 🧪

> Point at your repo and your live site. Get a grounded, runnable E2E suite.

Testra generates complete, runnable test scripts for **Playwright**, **Cypress**, or **Selenium** — with Page Object Models, CI/CD pipelines, inline comments, and every selector traced back to something that provably exists in your app.

Free tier included (a monthly generation allowance, no card required); Pro removes the cap and routes to a stronger model. An account is required, because publishing pushes to GitHub on your behalf.

---

## ✨ Features

- **CLI for the terminal** — `npx @testra/cli publish --with-ci` from inside your project (not yet on npm — run from a clone for now, see [`cli/`](cli/README.md)). Your `.gitignore` decides what uploads, and credentials are scanned *before* anything is sent. Zero dependencies. See [`cli/`](cli/README.md).
- **Two generation modes.** *Generate* is crawl-first: it renders and crawls your deployed site and writes tests from the real DOM — your repo's file contents never reach the model. *Publish* is source-first: upload a ZIP, get a new GitHub repo with the project plus a validated suite and working CI.
- **Grounded selectors, with provenance.** Every id, test-id and class the model writes is checked back against your real source or live DOM, and the UI cites the file and line it came from rather than asking for trust.
- **Self-heal loop.** Files that don't parse, and selectors that don't resolve, are sent back to the model *with the real anchors that do exist* — a grounded substitution, not a second guess.
- **Agent mode (Pro).** Instead of a scripted plan → generate → heal pipeline, the model investigates the app itself: it searches the source, reads the files it chooses, queries the grounding index for selectors that provably exist, and validates each file before committing it. Runs on **Claude or Gemini** — whichever the tier prefers and has capacity — and every failure mode falls back to the scripted pipeline, so it can only ever improve a run, never cost you one. Opt in with `agent_mode` on `POST /api/generate/{job_id}` or the publish form; the response's `agent` field reports the provider, turns and tool calls it actually made.
- **Optionally, it runs the tests.** With `ENABLE_TEST_EXECUTION=true` the agent executes the suite it just wrote against your deployed URL, reads the real Playwright failures, and fixes them — the one check here that can say a suite *passes* rather than merely parses. Off by default, because it runs model-written code on the host; see [`.env.example`](backend/.env.example) for what that means before enabling it.
- **Generation success rate.** One 0–100 score with a full breakdown of the checks behind it. Explicitly not a pass rate — see [Limitations](docs/LIMITATIONS.md).
- **Credentials never leave your machine.** `.env`, SSH keys, `.pem`, cloud service-account JSONs and hard-coded API tokens are detected and withheld from both the model prompt and the push — and every withheld file is named, never silently dropped.
- **Verified pushes.** After publishing, the commit tree is read back from GitHub and every file confirmed present with the exact content hash uploaded. Missing files are re-pushed; anything still missing fails loudly.
- **Smart LLM rotation** — rotates across Gemini (`gemini-3.6-flash`) → Groq (`llama-3.3-70b-versatile`) → Claude (`claude-haiku-4-5`), with per-key cooldowns on 429. The provenance report always names the model that actually answered.
- **Multi-framework** — Playwright (JS/TS/Python), Cypress (JS/TS), Selenium (Python/Java)
- **Security scanning** — passive configuration audit of a deployed URL, plus optional active scanning via OWASP ZAP.
- **Real-time streaming** — watch tests being written token by token.

---

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- Node.js 20+
- Docker (optional, for docker-compose)

### 1. Clone the repo

```bash
git clone https://github.com/your-org/testra
cd testra
```

### 2. Set up API keys (free — no credit card required)

```bash
cp backend/.env.example backend/.env
```

Get your free API keys:
| Provider | Free Tier | Get Key |
|----------|-----------|---------|
| Google Gemini (`gemini-3.6-flash`) | Large context, generous free tier | [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| Groq (`llama-3.3-70b-versatile`) | Ultra-fast, generous limits | [console.groq.com](https://console.groq.com) |
| Anthropic (`claude-haiku-4-5`) | Reliable fallback | [console.anthropic.com](https://console.anthropic.com) |

You only need **one** key to get started. Add more for better rate limit handling.

Edit `backend/.env`:
```env
GEMINI_API_KEY_1=your_key_here
GROQ_API_KEY_1=your_key_here
# etc.
```

### 3. Run with Docker Compose (recommended)

```bash
docker compose up
```

Frontend: http://localhost:5173  
Backend API: http://localhost:8000  
API docs: http://localhost:8000/docs

**With active vulnerability scanning** (starts an OWASP ZAP daemon alongside):

```bash
docker compose --profile zap up
```

ZAP takes about a minute to answer after it starts — the backend waits for it
rather than quietly falling back to the passive audit. Without the profile,
active scans degrade to the passive configuration audit and say so.
`GET /api/scan/capabilities` reports whether it's ready, and why not if it isn't.

### 3a. Publishing to GitHub

Publishing pushes to a repo on your behalf, so the app needs GitHub access. It
asks for that with **Sign in with GitHub** — no Personal Access Token to paste.
Create an OAuth App at <https://github.com/settings/developers> with the callback
URL `http://localhost:8000/api/auth/github/callback`, and put the credentials in
`backend/.env`:

```env
GITHUB_CLIENT_ID=Ov23li...
GITHUB_CLIENT_SECRET=...
```

Already signed in with a password? The publish page's *Sign in with GitHub*
button attaches GitHub to that same account and brings you straight back — you
keep your session, history and plan. (Without these two values the server has no
OAuth app to send you to, and the page falls back to asking for a token.)

### 3b. Run manually

**Backend:**
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

---

## 🏗️ Architecture

```
testra/
├── frontend/                   # React + Vite + Tailwind + React Router
│   └── src/
│       ├── pages/              # One per route: Home, Generate, Publish, Scan,
│       │                       #   Dashboard, Settings, auth flows, admin console
│       ├── components/
│       │   ├── GenerateInput.tsx    # Repo URL + hosted URL + optional site login
│       │   ├── ConfigureStep.tsx    # Framework / language / flows
│       │   ├── StreamingOutput.tsx  # Live token streaming
│       │   ├── ResultsStep.tsx      # The generated suite + downloads
│       │   ├── SuccessRate.tsx      # Headline score + its breakdown
│       │   ├── TrustPanel.tsx       # What we checked, and what we didn't
│       │   ├── AgentTrace.tsx       # What the agent did, and what it ran
│       │   ├── ExcludedSecrets.tsx  # Credential files we withheld
│       │   ├── PublishPanel.tsx     # ZIP → new GitHub repo
│       │   ├── ScanPanel.tsx        # Security scan of a deployed URL
│       │   └── FlowPipeline.tsx     # Live per-step progress
│       ├── context/AuthContext.tsx
│       └── utils/{api,download,format}.ts
│
├── backend/                    # FastAPI (Python)
│   ├── agents/
│   │   ├── filter_agent.py     # Agent 1: file filtering + project analysis
│   │   ├── writer_agent.py     # Agent 2: test generation, grounding, self-heal
│   │   └── scaffold.py         # Templated configs, CI pipelines, README
│   ├── services/
│   │   ├── llm_router.py       # ⭐ Multi-provider rotation + provenance
│   │   ├── agent_loop.py       # Agent mode: the model drives, with tools
│   │   ├── agent_tools.py      # The tools it's allowed to use
│   │   ├── agent_protocol.py   # One conversation → each provider's shape
│   │   ├── test_runner.py      # Actually runs the suite (opt-in)
│   │   ├── file_extractor.py   # File filtering, scoring, push preparation
│   │   ├── importance_model.py # From-scratch logistic regression (file value)
│   │   ├── live_crawler.py     # Renders + crawls the deployed site
│   │   ├── grounding.py        # Selector → source/DOM provenance
│   │   ├── fragility.py        # Selector durability scoring
│   │   ├── validator.py        # Static validation of generated files
│   │   ├── success_rate.py     # The headline score
│   │   ├── secrets_guard.py    # Credential detection + exclusion
│   │   ├── safe_paths.py       # Safe filenames, no-overwrite merging
│   │   ├── git_publisher.py    # Create repo, push, verify every file landed
│   │   ├── security_scanner.py # Passive audit;  zap_scanner.py = active
│   │   ├── store.py            # Durable SQLite job store
│   │   └── auth / quota / billing / admin / mailer / progress
│   ├── models/schemas.py       # Pydantic request/response models
│   ├── tests/                  # 1169 tests across 39 suites
│   └── main.py                 # FastAPI app + all routes
│
├── cli/                        # `npx @testra/cli` — zero runtime dependencies
│   ├── bin/testra.js           # Entry point + arg parsing
│   └── src/
│       ├── collect.js          # git ls-files → what belongs to the project
│       ├── secrets.js          # Local credential scan, before upload
│       ├── zip.js              # Minimal ZIP writer over node:zlib
│       ├── api.js              # HTTP + NDJSON progress stream
│       └── commands/           # publish · login/logout/whoami
│
├── site/                       # Static landing site (deployable to GitHub Pages)
├── docs/                       # ARCHITECTURE.md · DEPLOY.md · LIMITATIONS.md
└── docker-compose.yml
```

### LLM Rotation Logic

```
Request comes in
     │
     ▼
Try Gemini flash (key 1)
  ├── ✅ Success → return result
  └── ❌ Rate limit → mark key exhausted (60s cooldown)
         │
         ▼
     Try Gemini key 2 (if configured)
       ├── ✅ Success → return result
       └── ❌ All Gemini keys exhausted
              │
              ▼
          Try Groq llama-3.3-70b (key 1)
            ├── ✅ Success → return result
            └── ❌ Rate limit → next key...
                   │
                   ▼
               Try Claude Haiku
                                         │
                                         └── ❌ AllProvidersExhausted exception
```

### Token Budget Management

Files are scored 1–10 by importance:
- **10**: Router files (App.jsx, router/index.js, urls.py)
- **9**: Page components (pages/, views/, screens/)
- **8**: Auth/form/checkout components
- **7**: Layout components
- **6**: Shared components
- **5**: Config files (package.json, vite.config.js)
- **4**: Utility files

When the repo exceeds the 600k token budget:
1. Low-importance files are **summarized** (extract IDs, classes, routes) instead of dropped
2. A warning is shown in the UI
3. The LLM still gets enough context to generate real selectors

---

## 🔌 API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/analyze-crawl` | **Crawl-first**: render + crawl the hosted site, create the job |
| `POST` | `/api/analyze` | Source-first: fetch repo, run Filter Agent, return project analysis |
| `POST` | `/api/upload-zip` | Same as `/api/analyze` but from a ZIP upload |
| `POST` | `/api/crawl-preview` | Run the crawler standalone and report what it saw |
| `GET` | `/api/stream/{job_id}` | SSE stream of Agent 2's output, token by token |
| `POST` | `/api/generate/{job_id}` | Run Agent 2, ground, self-heal, validate, score, return files |
| `POST` | `/api/publish-zip` | ZIP → brand-new GitHub repo (+ optional suite & CI), verified |
| `GET` | `/api/repos` · `DELETE /api/repo/{owner}/{repo}` | List / undo published repos |
| `POST` | `/api/scan` · `GET /api/scan/capabilities` | Security scan a deployed URL |
| `GET` | `/api/status` | LLM provider health + key availability |
| `GET` | `/api/dashboard` · `/api/profile` · `/api/settings` | Account surfaces |
| `GET` | `/health` · `/metrics` | Health check and metrics |

Auth (`/api/auth/*` — signup, login, OTP, password reset, GitHub and Google
OAuth), billing (`/api/billing/*`) and the admin console (`/api/admin/*`) are
documented in the interactive schema.

The streaming endpoints report per-step progress as NDJSON over the request that
started them, so anything decidable up front (a malformed URL, a corrupt archive)
is still a plain HTTP error rather than an in-band one.

Full interactive docs: http://localhost:8000/docs

---

## 🧪 Running Tests

```bash
cd backend
pytest tests/ -q          # 1169 tests across 39 suites
ruff check .
```

Frontend:

```bash
cd frontend
npx tsc --noEmit && npm run build
```

CLI:

```bash
cd cli
npm test
```

---

## 🌐 Landing site

`site/index.html` is a standalone, dependency-free page presenting the project —
deployable as-is to GitHub Pages (Settings → Pages → deploy from `master`,
`/site`), or served locally with:

```bash
cd site && python3 -m http.server
```

---

## 🚢 Deployment

Production runs as containers behind [Caddy](https://caddyserver.com) (automatic
TLS + static serving) on a single host. The scripts in `deploy/` provision it:

```bash
deploy/aws-setup.sh        # provision the host
deploy/bootstrap.sh        # install docker, pull, first boot
# topology: deploy/docker-compose.prod.yml
```

**Full walkthrough — including the CI deploy-over-SSH job, DNS, and the ZAP
sidecar — is in [docs/DEPLOY.md](docs/DEPLOY.md).**

The frontend needs `VITE_API_URL` pointed at the API's public origin at build
time. Note that the crawl-first generate flow needs a **headless browser on the
server** (Playwright + Chromium, installed by `backend/Dockerfile`); without one,
`/api/analyze-crawl` returns a 503 that says so rather than silently falling back.

---

## 🗺️ Roadmap

Shipped:

- [x] Crawl-first generation from the live DOM · ZIP upload · publish to a new repo
- [x] Zero-dependency CLI (`npx @testra/cli`) with device-code login
- [x] Two-agent pipeline · multi-provider key rotation with provenance
- [x] Playwright / Cypress / Selenium, Page Object Models, GitHub Actions + GitLab CI
- [x] Selector grounding with file:line provenance, and the self-heal loop
- [x] Fragility (durability) grading · generation success rate
- [x] Credential exclusion · safe filenames · verified pushes
- [x] Hashed session tokens · per-account login throttling · zip-bomb guard
- [x] Accounts, quota and plans · security scanning (passive + ZAP)

Next, in order of how much each would change what the product can honestly claim
(full detail in the [project report](Testra-Report.html), §23):

- [ ] **Sandboxed execution** — run the suite in Docker against a live target and
      report real pass/fail, turning the success rate into a measured outcome
- [ ] **AST-based selector extraction** — replace regex, capturing roles,
      accessible names, visible text and dynamic classes
- [ ] **Entropy-based secret scanning** — catch bespoke credentials, not just
      issuer-prefixed tokens
- [ ] **Retrieval-based context selection** — stop relying on summarization for
      very large repos
- [ ] **Re-fit both ML models on real outcomes** (the plumbing already exists)
- [ ] **Incremental publishing** — push to an existing repo on a branch, with a PR
- [ ] **Diff-aware regeneration** · **conversational refinement** ·
      **self-healing suites over time** · **visual selector picking**

---

## 🤝 Contributing

PRs welcome! The codebase is intentionally straightforward:

1. **Adding a new LLM provider**: add a `_call_<provider>` method in `llm_router.py` and add it to `PROVIDER_PRIORITY`
2. **Adding a new test framework**: add instructions to `FRAMEWORK_INSTRUCTIONS` in `writer_agent.py`
3. **Improving file filtering**: edit `UI_EXTENSIONS`, `SKIP_PATTERNS`, or `score_file()` in `file_extractor.py`
4. **Teaching it a new kind of secret**: add to `SECRET_FILENAMES` / `CONTENT_RULES` in `services/secrets_guard.py`. Content rules must be issuer-prefixed or otherwise unambiguous — a rule with false positives drops real source and gets the whole guard switched off. Add the case to `tests/test_secrets_guard.py` from the *leak's* point of view, and a counter-case proving ordinary code still passes.
5. **Changing what the success rate measures**: `services/success_rate.py`. A component with nothing to measure must be dropped, never scored 100% — see the tests for why.

---

## 📄 License

MIT — free to use, modify, and deploy.
