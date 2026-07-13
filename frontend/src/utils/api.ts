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
async function parseError(res: Response, fallback: string): Promise<never> {
  const err = await res.json().catch(() => ({ detail: res.statusText }));
  throw new Error(err.detail || fallback);
}

export interface User {
  id: string;
  email?: string | null;
  name?: string | null;
  github_login?: string | null;
  avatar_url?: string | null;
  has_github: boolean;
  has_google: boolean;
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

export async function fetchMe(): Promise<User | null> {
  if (!getToken()) return null;
  const res = await fetch(`${API_BASE}/api/auth/me`, { headers: authHeaders() });
  if (!res.ok) return null;
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

export function googleLoginUrl(): string {
  return `${API_BASE}/api/auth/google/login`;
}

export async function getAuthConfig(): Promise<{ github_oauth_enabled: boolean; google_oauth_enabled: boolean }> {
  const res = await fetch(`${API_BASE}/api/auth/config`);
  if (!res.ok) return { github_oauth_enabled: false, google_oauth_enabled: false };
  return res.json();
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
