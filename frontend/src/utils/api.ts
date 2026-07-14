const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

// ── Auth token management ────────────────────
const TOKEN_KEY = "tg_token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}
function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const t = getToken();
  return t ? { ...extra, Authorization: `Bearer ${t}` } : extra;
}
export interface QuotaInfo {
  plan: string;
  used: number;
  limit: number | null;
  remaining: number | null;
  period: string;
  resets_at: number;
}

/** Thrown on 402 — the user has spent their monthly free generations.
 *  This is the ONLY error that may open the upgrade modal. A provider outage
 *  (503) is not an upsell: paying wouldn't fix it. */
export class QuotaExceededError extends Error {
  used: number;
  limit: number | null;
  resetsAt: number;
  pricing: { monthly_usd: number; yearly_usd: number };
  constructor(detail: any) {
    super(detail?.message || "You've used all your free generations this month.");
    this.name = "QuotaExceededError";
    this.used = detail?.used ?? 0;
    this.limit = detail?.limit ?? null;
    this.resetsAt = detail?.resets_at ?? 0;
    this.pricing = detail?.pricing || { monthly_usd: 20, yearly_usd: 100 };
  }
}

async function parseError(res: Response, fallback: string): Promise<never> {
  const err = await res.json().catch(() => ({ detail: res.statusText }));
  let detail = err?.detail;
  // 402 carries a structured quota payload, not a message string.
  if (res.status === 402 && detail && typeof detail === "object") {
    throw new QuotaExceededError(detail);
  }
  // FastAPI/Pydantic returns `detail` as an array of {loc, msg, ...} for
  // validation (422) errors — flatten it to a readable string instead of
  // letting `new Error([...])` stringify to "[object Object]".
  if (Array.isArray(detail)) {
    detail = detail
      .map((d) => (typeof d === "string" ? d : d?.msg || JSON.stringify(d)))
      .join("; ");
  } else if (detail && typeof detail === "object") {
    detail = detail.msg || JSON.stringify(detail);
  }
  throw new Error(detail || fallback);
}

export interface User {
  id: string;
  email?: string | null;
  name?: string | null;
  github_login?: string | null;
  avatar_url?: string | null;
  has_github: boolean;
  plan?: "free" | "pro";
}

export async function signup(email: string, password: string, name?: string): Promise<{ token: string; user: User }> {
  const res = await fetch(`${API_BASE}/api/auth/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, name }),
  });
  if (!res.ok) await parseError(res, "Sign up failed");
  const data = await res.json();
  setToken(data.token);
  return data;
}

export async function login(email: string, password: string): Promise<{ token: string; user: User }> {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) await parseError(res, "Login failed");
  const data = await res.json();
  setToken(data.token);
  return data;
}

/** Thrown when /me couldn't be reached or answered — as opposed to answering
 *  "you are not logged in". Callers must not clear the session on this. */
export class AuthUnavailableError extends Error {}

export async function fetchMe(): Promise<User | null> {
  if (!getToken()) return null;
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/auth/me`, { headers: authHeaders() });
  } catch {
    throw new AuthUnavailableError("Could not reach the server.");
  }
  // Only GitHub's own verdict on the token ends a session. A 5xx means the
  // server is unwell, not that the token is bad — don't sign the user out.
  if (res.status === 401 || res.status === 403) return null;
  if (!res.ok) throw new AuthUnavailableError(`Server returned ${res.status}.`);
  const data = await res.json();
  return data.authenticated ? data.user : null;
}

export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/api/auth/logout`, { method: "POST", headers: authHeaders() }).catch(() => {});
  setToken("");
}

export function githubLoginUrl(): string {
  return `${API_BASE}/api/auth/github/login`;
}

export async function getAuthConfig(): Promise<{ github_oauth_enabled: boolean; max_upload_mb: number }> {
  const res = await fetch(`${API_BASE}/api/auth/config`);
  if (!res.ok) return { github_oauth_enabled: false, max_upload_mb: maxUploadMb };
  const cfg = await res.json();
  if (typeof cfg.max_upload_mb === "number") maxUploadMb = cfg.max_upload_mb;
  return cfg;
}

// ── Test generation ──────────────────────────

export async function analyzeRepo(
  repoUrl: string, framework: string, language: string, testFlows: string, baseUrl: string, githubToken?: string
): Promise<{ job_id: string; analysis: any }> {
  const res = await fetch(`${API_BASE}/api/analyze`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ repo_url: repoUrl, framework, language, test_flows: testFlows, base_url: baseUrl, github_token: githubToken || undefined }),
  });
  if (!res.ok) await parseError(res, "Analysis failed");
  return res.json();
}

// Server-enforced upload cap (MAX_REPO_SIZE_MB). Refreshed by getAuthConfig();
// the fallback only matters before that resolves, and the server re-checks anyway.
let maxUploadMb = 50;

/** Reason `file` can't be uploaded, or null if it's fine. */
export function validateZip(file: File): string | null {
  if (!file.name.endsWith(".zip")) return "Only .zip files are supported";
  const mb = file.size / 1024 / 1024;
  if (mb > maxUploadMb) {
    return `ZIP is ${mb.toFixed(0)} MB — the limit is ${maxUploadMb} MB. Exclude dependency folders (venv/, node_modules/) from the archive.`;
  }
  return null;
}

export async function uploadZip(
  file: File, framework: string, language: string, testFlows: string, baseUrl: string
): Promise<{ job_id: string; analysis: any }> {
  const form = new FormData();
  form.append("file", file);
  form.append("framework", framework);
  form.append("language", language);
  form.append("test_flows", testFlows);
  form.append("base_url", baseUrl);
  const res = await fetch(`${API_BASE}/api/upload-zip`, { method: "POST", headers: authHeaders(), body: form });
  if (!res.ok) await parseError(res, "Upload failed");
  return res.json();
}

export async function generateTests(
  jobId: string, framework: string, language: string, testFlows: string, baseUrl: string, includeCi: boolean
): Promise<any> {
  const res = await fetch(`${API_BASE}/api/generate/${jobId}`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ framework, language, test_flows: testFlows, base_url: baseUrl, include_ci: includeCi }),
  });
  if (!res.ok) await parseError(res, "Generation failed");
  return res.json();
}

export function streamGeneration(
  jobId: string, framework: string, language: string, testFlows: string, baseUrl: string,
  onChunk: (chunk: string) => void, onProvider: (provider: string) => void,
  onDone: () => void, onError: (err: string) => void
): () => void {
  // EventSource can't set headers, so the auth token rides as a query param.
  const params = new URLSearchParams({ framework, language, test_flows: testFlows, base_url: baseUrl, token: getToken() });
  const es = new EventSource(`${API_BASE}/api/stream/${jobId}?${params}`);
  es.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "chunk") onChunk(msg.content);
      else if (msg.type === "provider_status") onProvider(msg.message);
      else if (msg.type === "done") { onDone(); es.close(); }
      else if (msg.type === "error") { onError(msg.message); es.close(); }
    } catch {}
  };
  es.onerror = () => { onError("Stream connection lost"); es.close(); };
  return () => es.close();
}

// ── Security scan ────────────────────────────

export async function scanUrl(url: string): Promise<any> {
  const res = await fetch(`${API_BASE}/api/scan`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ url, ai_summary: true }),
  });
  if (!res.ok) await parseError(res, "Scan failed");
  return res.json();
}

// ── Publish ──────────────────────────────────

export interface PublishOptions {
  githubToken?: string;    // PAT fallback when OAuth isn't configured
  repoName: string;
  addCicd: boolean;
  private: boolean;
  framework?: string;
  language?: string;
  testFlows?: string;
  baseUrl?: string;
  repoDescription?: string;
}

export async function publishZip(file: File, opts: PublishOptions): Promise<any> {
  const form = new FormData();
  form.append("file", file);
  form.append("github_token", opts.githubToken || "");
  form.append("repo_name", opts.repoName);
  form.append("add_cicd", String(opts.addCicd));
  form.append("private", String(opts.private));
  form.append("framework", opts.framework || "playwright");
  form.append("language", opts.language || "typescript");
  form.append("test_flows", opts.testFlows || "");
  form.append("base_url", opts.baseUrl || "http://localhost:3000");
  form.append("repo_description", opts.repoDescription || "");
  const res = await fetch(`${API_BASE}/api/publish-zip`, { method: "POST", headers: authHeaders(), body: form });
  if (!res.ok) await parseError(res, "Publish failed");
  return res.json();
}

/** Delete a repo Testra created. Irreversible; `fullName` is echoed back as confirmation. */
export async function deleteRepo(fullName: string): Promise<{ deleted: string }> {
  const res = await fetch(
    `${API_BASE}/api/repo/${fullName}?confirm=${encodeURIComponent(fullName)}`,
    { method: "DELETE", headers: authHeaders() }
  );
  if (!res.ok) await parseError(res, "Could not delete the repository");
  return res.json();
}

// ── Billing ──────────────────────────────────

export interface Plan {
  id: "monthly" | "yearly";
  name: string;
  price_usd: number;
  interval: string;
  checkout_url: string | null;
  savings_usd?: number;
}

export interface BillingInfo {
  quota: QuotaInfo;
  plans: Plan[];
  checkout_available: boolean;
  contact_email: string | null;
}

export async function getBillingInfo(): Promise<BillingInfo> {
  const res = await fetch(`${API_BASE}/api/billing/me`, { headers: authHeaders() });
  if (!res.ok) await parseError(res, "Could not load your plan");
  return res.json();
}

/** Thrown when no payment provider is configured on the server yet. */
export class CheckoutUnavailableError extends Error {}

/** Ask the server for a hosted checkout URL. The server owns the link so the
 *  user id can be attached for the webhook that grants the plan. */
export async function startCheckout(plan: "monthly" | "yearly"): Promise<string> {
  const form = new FormData();
  form.append("plan", plan);
  const res = await fetch(`${API_BASE}/api/billing/checkout`, {
    method: "POST", headers: authHeaders(), body: form,
  });
  if (res.status === 503) {
    const err = await res.json().catch(() => ({}));
    throw new CheckoutUnavailableError(err?.detail || "Checkout isn't available yet.");
  }
  if (!res.ok) await parseError(res, "Could not start checkout");
  const data = await res.json();
  return data.checkout_url;
}

// ── Status ───────────────────────────────────

export async function getProviderStatus(): Promise<any> {
  const res = await fetch(`${API_BASE}/api/status`, { headers: authHeaders() });
  if (!res.ok) throw new Error("Status check failed");
  return res.json();
}

export async function getMetrics(): Promise<any> {
  const res = await fetch(`${API_BASE}/metrics`);
  if (!res.ok) throw new Error("Metrics unavailable");
  return res.json();
}
