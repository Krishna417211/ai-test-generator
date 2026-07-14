# Testra 🧪

> Paste a GitHub URL. Get production-ready E2E tests in seconds.

Testra scans your web project's codebase and automatically generates complete, runnable test scripts for **Playwright**, **Cypress**, or **Selenium** — with real selectors from your actual source code, Page Object Models, CI/CD pipelines, and inline comments.

**100% free. No account. No credit card.**

---

## ✨ Features

- **Two-agent LLM pipeline** — Agent 1 filters your repo to UI-relevant files only. Agent 2 writes tests with real selectors from your code.
- **Smart LLM rotation** — Automatically rotates through Gemini 1.5 Flash → Groq LLaMA 3.1 → Claude Haiku. Never hits rate limits.
- **Multi-framework** — Playwright (JS/TS/Python), Cypress (JS/TS), Selenium (Python/Java)
- **Page Object Model** — Generates POM classes, not spaghetti scripts
- **CI/CD included** — GitHub Actions + GitLab CI yaml bundled in every download
- **Selector validation** — Post-processing pass flags hallucinated selectors with ⚠️ warnings
- **GitHub public + private repos** — Paste a URL or upload a ZIP
- **Real-time streaming** — Watch tests being written token by token

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
| Google Gemini 1.5 Flash | 1M context, 15 RPM | [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| Groq (LLaMA 3.1 70B) | Ultra-fast, generous limits | [console.groq.com](https://console.groq.com) |
| Anthropic Claude Haiku | Reliable fallback | [console.anthropic.com](https://console.anthropic.com) |

You only need **one** key to get started. Add more for better rate limit handling.

Edit `backend/.env`:
```env
GEMINI_API_KEY_1=your_key_here
GROQ_API_KEY_1=your_key_here
# etc.
```

### 3. Run with Docker Compose (recommended)

```bash
docker-compose up
```

Frontend: http://localhost:5173  
Backend API: http://localhost:8000  
API docs: http://localhost:8000/docs

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
├── frontend/                  # React + Vite + Tailwind
│   └── src/
│       ├── components/
│       │   ├── InputStep.tsx       # Step 1: GitHub URL / ZIP upload
│       │   ├── PreviewStep.tsx     # Step 2: Project analysis preview
│       │   ├── ConfigureStep.tsx   # Step 3: Framework/language config
│       │   ├── StreamingOutput.tsx # Step 4: Live token streaming
│       │   ├── ResultsStep.tsx     # Step 5: Download generated tests
│       │   ├── StepIndicator.tsx   # Wizard progress bar
│       │   └── ProviderStatus.tsx  # LLM health dashboard
│       └── utils/
│           ├── api.ts              # All backend API calls
│           └── download.ts         # ZIP/file download helpers
│
├── backend/                   # FastAPI (Python)
│   ├── agents/
│   │   ├── filter_agent.py     # Agent 1: file filtering + project analysis
│   │   └── writer_agent.py     # Agent 2: test script generation
│   ├── services/
│   │   ├── llm_router.py       # ⭐ Multi-provider rotation engine
│   │   ├── github_service.py   # GitHub API + raw file fetching
│   │   └── file_extractor.py   # Smart file filtering + scoring
│   ├── models/
│   │   └── schemas.py          # Pydantic request/response models
│   ├── tests/
│   │   └── test_file_extractor.py
│   └── main.py                 # FastAPI app + all routes
│
└── docker-compose.yml
```

### LLM Rotation Logic

```
Request comes in
     │
     ▼
Try Gemini 1.5 Flash (key 1)
  ├── ✅ Success → return result
  └── ❌ Rate limit → mark key exhausted (60s cooldown)
         │
         ▼
     Try Gemini key 2 (if configured)
       ├── ✅ Success → return result
       └── ❌ All Gemini keys exhausted
              │
              ▼
          Try Groq LLaMA 3.1 (key 1)
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
| `POST` | `/api/analyze` | Fetch repo, run Filter Agent, return project analysis |
| `POST` | `/api/upload-zip` | Same as above but from ZIP file upload |
| `POST` | `/api/generate/{job_id}` | Run Writer Agent, return generated test files |
| `GET` | `/api/stream/{job_id}` | SSE stream for real-time generation output |
| `GET` | `/api/status` | LLM provider health + key availability |
| `GET` | `/api/logs` | Recent API call log |
| `GET` | `/health` | Basic health check |

Full interactive docs: http://localhost:8000/docs

---

## 🧪 Running Tests

```bash
cd backend
pytest tests/ -v
```

---

## 🚢 Deployment (Free)

### Backend → Render.com (free tier)
1. Push to GitHub
2. New Web Service → connect repo → set root to `backend/`
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Add environment variables from `.env`

### Frontend → Vercel (free tier)
1. Import frontend/ directory to Vercel
2. Set `VITE_API_URL` environment variable to your Render backend URL
3. Deploy

---

## 🗺️ Roadmap

### Phase 1 — Core MVP ✅
- [x] GitHub URL input + ZIP upload
- [x] Two-agent LLM pipeline
- [x] Multi-provider key rotation
- [x] Playwright / Cypress / Selenium output
- [x] Page Object Model generation
- [x] CI/CD yaml (GitHub Actions + GitLab)
- [x] Real-time streaming output
- [x] ZIP download of all generated files

### Phase 2 — Smart Features
- [ ] Selector validation pass (post-generation)
- [ ] Multiple language options per framework
- [ ] Show file tree preview before generating
- [ ] Live API status indicator in UI

### Phase 3 — God Mode
- [ ] Self-healing tests (LLM suggests alternatives for broken selectors)
- [ ] Diff detection (re-upload new version → see which tests need updating)
- [ ] Auto-run tests in sandboxed Playwright (Docker + Railway free tier)
- [ ] Chat interface: "add a test for forgot password flow"
- [ ] Visual selector picker: screenshot + highlight → auto-generate locators
- [ ] Multi-LLM comparison mode

---

## 🤝 Contributing

PRs welcome! The codebase is intentionally straightforward:

1. **Adding a new LLM provider**: add a `_call_<provider>` method in `llm_router.py` and add it to `PROVIDER_PRIORITY`
2. **Adding a new test framework**: add instructions to `FRAMEWORK_INSTRUCTIONS` in `writer_agent.py`
3. **Improving file filtering**: edit `UI_EXTENSIONS`, `SKIP_PATTERNS`, or `score_file()` in `file_extractor.py`

---

## 📄 License

MIT — free to use, modify, and deploy.
