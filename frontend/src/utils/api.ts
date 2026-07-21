// Type-only, and it must stay that way: types/index.ts imports `User` from this
// file, so a value import here would close the cycle. `import type` is erased at
// build time, so the two files can describe each other's shapes without one.
import type { Provenance } from "../types";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

// ── Auth token management ────────────────────
//
// The token lives in sessionStorage, not localStorage: it must not outlive the
// window. sessionStorage is cleared by the browser when the last tab of an
// origin closes, which is exactly the requirement — close the window, you're
// logged out — and it's enforced by the browser rather than by us remembering
// to expire something.
//
// The cost is that sessionStorage is per-tab, so a new tab (or a middle-click
// on a link) starts blank and would show a logged-out app while the original
// tab is still signed in. The handshake below fixes that by asking the other
// tabs for the token. If no tab answers, there is nothing to inherit — which is
// the closed-the-window case, and staying logged out is the correct outcome.
const TOKEN_KEY = "tg_token";

// localStorage keys used only as a postMessage-style bus between tabs. `storage`
// events fire in *other* tabs of the same origin, never the one that wrote —
// which is what makes this work as a request/response.
const SHARE_REQUEST_KEY = "tg_session_request";
const SHARE_REPLY_KEY = "tg_session_reply";
const LOGOUT_BROADCAST_KEY = "tg_logout";

export function getToken(): string {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(token: string): void {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

/** Write a value for other tabs, then immediately remove it.
 *
 *  The `storage` event other tabs receive carries the value as it was written,
 *  so removing it on the next line doesn't race them — they still see it. This
 *  keeps the token from lingering in localStorage, which would defeat the whole
 *  point of sessionStorage: a leftover copy would survive the window closing.
 */
function broadcast(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
    localStorage.removeItem(key);
  } catch {
    // Private mode / storage disabled: tab sync degrades to "log in again",
    // which is inconvenient but not broken.
  }
}

/** Answer other tabs' requests for the session. Call once, at startup. */
export function serveSessionToOtherTabs(): () => void {
  const onStorage = (e: StorageEvent) => {
    if (e.key !== SHARE_REQUEST_KEY || !e.newValue) return;
    const token = getToken();
    if (token) broadcast(SHARE_REPLY_KEY, token);
  };
  window.addEventListener("storage", onStorage);
  return () => window.removeEventListener("storage", onStorage);
}

/** Ask any open tab for the current session token. Resolves to "" if none answers.
 *
 *  The timeout is what distinguishes "another tab has a session" from "this is a
 *  fresh window and nobody is logged in" — there's no way to enumerate tabs, so
 *  silence is the only available signal.
 */
export function requestSessionFromOtherTabs(timeoutMs = 250): Promise<string> {
  if (getToken()) return Promise.resolve(getToken());

  return new Promise((resolve) => {
    let done = false;
    const finish = (token: string) => {
      if (done) return;
      done = true;
      window.removeEventListener("storage", onReply);
      clearTimeout(timer);
      resolve(token);
    };
    const onReply = (e: StorageEvent) => {
      if (e.key !== SHARE_REPLY_KEY || !e.newValue) return;
      setToken(e.newValue);
      finish(e.newValue);
    };
    const timer = setTimeout(() => finish(""), timeoutMs);

    window.addEventListener("storage", onReply);
    broadcast(SHARE_REQUEST_KEY, String(Date.now()));
  });
}

/** Tell other tabs to drop their session. */
export function broadcastLogout(): void {
  broadcast(LOGOUT_BROADCAST_KEY, String(Date.now()));
}

/** React to another tab logging out. Call once, at startup. */
export function onLogoutElsewhere(handler: () => void): () => void {
  const onStorage = (e: StorageEvent) => {
    if (e.key !== LOGOUT_BROADCAST_KEY || !e.newValue) return;
    setToken("");
    handler();
  };
  window.addEventListener("storage", onStorage);
  return () => window.removeEventListener("storage", onStorage);
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

/** Turn a status + FastAPI `detail` into the right exception.
 *
 *  Split out of parseError because a streamed endpoint reports failures in its
 *  body rather than its status (see backend/services/progress.py): once the
 *  stream opens the status is already 200. Both paths land here so an in-band
 *  error is indistinguishable from an HTTP one — a 402 still opens the upgrade
 *  modal whether it arrived as a status or as a line of NDJSON. */
function raiseApiError(status: number, detail: any, fallback: string): never {
  // 402 carries a structured quota payload, not a message string.
  if (status === 402 && detail && typeof detail === "object") {
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

async function parseError(res: Response, fallback: string): Promise<never> {
  const err = await res.json().catch(() => ({ detail: res.statusText }));
  raiseApiError(res.status, err?.detail, fallback);
}

/** State of one step in a live pipeline, as reported by the server. */
export type StepState = "running" | "done" | "skipped";
export interface StepEvent {
  id: string;
  state: StepState;
  detail?: string;
}
export type OnProgress = (event: StepEvent) => void;

/** POST to an NDJSON endpoint, reporting step events as they arrive and
 *  resolving with the final result.
 *
 *  Errors arrive two ways and both must behave like a normal failed request:
 *  a pre-flight rejection is still a real status (`res.ok` is false, nothing has
 *  streamed), and a mid-flight one is an `{"type":"error"}` line. */
async function postNdjson(
  path: string,
  init: RequestInit,
  onProgress: OnProgress | undefined,
  fallback: string,
): Promise<any> {
  const res = await fetch(`${API_BASE}${path}`, init);
  if (!res.ok) await parseError(res, fallback);
  if (!res.body) throw new Error(fallback);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  // Held on an object rather than in two `let`s: these are only ever assigned
  // from inside `handle`, and TypeScript's flow analysis doesn't follow a
  // closure — it would narrow a local to `null` and reject the read below.
  const out: { result?: any; failure?: { status: number; detail: any } } = {};

  const handle = (line: string) => {
    if (!line.trim()) return;
    let msg: any;
    try {
      msg = JSON.parse(line);
    } catch {
      return; // a partial line can't happen (we split on \n), so this is corrupt — skip it
    }
    if (msg.type === "step") onProgress?.({ id: msg.id, state: msg.state, detail: msg.detail });
    else if (msg.type === "result") out.result = msg.data;
    else if (msg.type === "error") out.failure = { status: msg.status, detail: msg.detail };
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    // The last element is whatever came after the final newline — an incomplete
    // line, or "". Keep it buffered until its newline arrives.
    buffer = lines.pop() ?? "";
    for (const line of lines) handle(line);
  }
  handle(buffer);

  if (out.failure) raiseApiError(out.failure.status, out.failure.detail, fallback);
  if (out.result === undefined) {
    // The stream ended without a result or an error — the connection dropped
    // mid-flight. Not a server error we can name, but not a success either.
    throw new Error("The connection dropped before the run finished. Please try again.");
  }
  return out.result;
}

export interface User {
  id: string;
  email?: string | null;
  name?: string | null;
  github_login?: string | null;
  avatar_url?: string | null;
  has_github: boolean;
  email_verified?: boolean;
  plan?: "free" | "pro";
  /** Whether to render the Admin nav item. A hint, not a permission: the server
   *  re-checks the allowlist on every /api/admin call, so flipping this in
   *  devtools only produces a page whose requests 404. */
  is_admin?: boolean;
}

/** Where a signup/login attempt landed. Mirrors LoginResponse in the backend:
 *  `ok` carries a session, the other two carry the next step instead. */
export interface LoginResult {
  status: "ok" | "otp_required" | "verification_required";
  token?: string | null;
  user?: User | null;
  challenge_id?: string | null;
  email_hint?: string | null;
  expires_in?: number | null;
  message?: string | null;
}

/** Store the token only for a completed login. A half-finished attempt has no
 *  token, and writing an undefined one would clear an existing session. */
function adoptIfComplete(data: LoginResult): LoginResult {
  if (data.status === "ok" && data.token) setToken(data.token);
  return data;
}

export async function signup(email: string, password: string, name?: string): Promise<LoginResult> {
  const res = await fetch(`${API_BASE}/api/auth/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, name }),
  });
  if (!res.ok) await parseError(res, "Sign up failed");
  return adoptIfComplete(await res.json());
}

export async function login(email: string, password: string): Promise<LoginResult> {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) await parseError(res, "Login failed");
  return adoptIfComplete(await res.json());
}

/** Second step of login: exchange the emailed 6-digit code for a session. */
export async function verifyLoginOtp(challengeId: string, code: string): Promise<{ token: string; user: User }> {
  const res = await fetch(`${API_BASE}/api/auth/login/verify-otp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ challenge_id: challengeId, code }),
  });
  if (!res.ok) await parseError(res, "That code didn't work");
  const data = await res.json();
  setToken(data.token);
  return data;
}

/** Redeem a verification link. Logs the user in on success. */
export async function verifyEmail(token: string): Promise<{ token: string; user: User }> {
  const res = await fetch(`${API_BASE}/api/auth/verify-email`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!res.ok) await parseError(res, "This verification link didn't work");
  const data = await res.json();
  setToken(data.token);
  return data;
}

export async function resendVerification(email: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/auth/resend-verification`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!res.ok) await parseError(res, "Could not resend the link");
  return (await res.json()).message || "";
}

export async function forgotPassword(email: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/auth/forgot-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!res.ok) await parseError(res, "Could not start the reset");
  return (await res.json()).message || "";
}

/** Set a new password from a reset link. Logs the user in on success. */
export async function resetPassword(token: string, password: string): Promise<{ token: string; user: User }> {
  const res = await fetch(`${API_BASE}/api/auth/reset-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, password }),
  });
  if (!res.ok) await parseError(res, "Could not reset your password");
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
  // Other tabs hold their own copy in their own sessionStorage; without this
  // they'd keep showing a signed-in UI against a token the server just revoked.
  broadcastLogout();
}

export function githubLoginUrl(): string {
  return `${API_BASE}/api/auth/github/login`;
}

export function googleLoginUrl(): string {
  return `${API_BASE}/api/auth/google/login`;
}

export interface AuthConfig {
  github_oauth_enabled: boolean;
  google_oauth_enabled: boolean;
  max_upload_mb: number;
  email_verification_enabled: boolean;
  login_otp_enabled: boolean;
  password_reset_enabled: boolean;
}

export async function getAuthConfig(): Promise<AuthConfig> {
  const fallback: AuthConfig = {
    github_oauth_enabled: false,
    google_oauth_enabled: false,
    max_upload_mb: maxUploadMb,
    // Assume the email flows are on when we can't ask: the UI only uses these
    // to decide what to offer, and hiding a real "forgot password" link is
    // worse than showing one that turns out to be unavailable.
    email_verification_enabled: true,
    login_otp_enabled: true,
    password_reset_enabled: true,
  };
  const res = await fetch(`${API_BASE}/api/auth/config`).catch(() => null);
  if (!res || !res.ok) return fallback;
  const cfg = await res.json();
  if (typeof cfg.max_upload_mb === "number") maxUploadMb = cfg.max_upload_mb;
  return { ...fallback, ...cfg };
}

// ── Test generation ──────────────────────────

export async function analyzeRepo(
  repoUrl: string, framework: string, language: string, testFlows: string, baseUrl: string,
  githubToken?: string, onProgress?: OnProgress
): Promise<{ job_id: string; analysis: any; provenance?: Provenance | null }> {
  return postNdjson("/api/analyze", {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ repo_url: repoUrl, framework, language, test_flows: testFlows, base_url: baseUrl, github_token: githubToken || undefined }),
  }, onProgress, "Analysis failed");
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
  file: File, framework: string, language: string, testFlows: string, baseUrl: string,
  onProgress?: OnProgress
): Promise<{ job_id: string; analysis: any; provenance?: Provenance | null }> {
  const form = new FormData();
  form.append("file", file);
  form.append("framework", framework);
  form.append("language", language);
  form.append("test_flows", testFlows);
  form.append("base_url", baseUrl);
  return postNdjson(
    "/api/upload-zip",
    { method: "POST", headers: authHeaders(), body: form },
    onProgress, "Upload failed",
  );
}

export async function generateTests(
  jobId: string, framework: string, language: string, testFlows: string, baseUrl: string,
  includeCi: boolean, liveUrl?: string
): Promise<any> {
  const res = await fetch(`${API_BASE}/api/generate/${jobId}`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      framework, language, test_flows: testFlows, base_url: baseUrl, include_ci: includeCi,
      // When present, the backend also verifies selectors against the live DOM
      // and self-heals the ones that miss. self_heal is turned on with it so the
      // grounding repair loop actually runs.
      live_url: liveUrl || undefined,
      self_heal: liveUrl ? true : undefined,
    }),
  });
  if (!res.ok) await parseError(res, "Generation failed");
  return res.json();
}

export function streamGeneration(
  jobId: string, framework: string, language: string, testFlows: string, baseUrl: string,
  onChunk: (chunk: string) => void, onProvider: (provider: string) => void,
  onDone: () => void, onError: (err: string) => void,
  // The stream now refuses an out-of-quota user before running the LLM, sending
  // an in-band error with reason="quota_exceeded" (EventSource can't read a 402
  // status). Route that to the upgrade modal, mirroring the /api/generate 402.
  onQuota?: (err: QuotaExceededError) => void
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
      else if (msg.type === "error") {
        es.close();
        if (onQuota && msg.detail?.reason === "quota_exceeded") onQuota(new QuotaExceededError(msg.detail));
        else onError(msg.message);
      }
    } catch {}
  };
  es.onerror = () => { onError("Stream connection lost"); es.close(); };
  return () => es.close();
}

// ── Security scan ────────────────────────────

export async function scanUrl(url: string, onProgress?: OnProgress): Promise<any> {
  return postNdjson("/api/scan", {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ url, ai_summary: true }),
  }, onProgress, "Scan failed");
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

export async function publishZip(
  file: File, opts: PublishOptions, onProgress?: OnProgress
): Promise<any> {
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
  return postNdjson(
    "/api/publish-zip",
    { method: "POST", headers: authHeaders(), body: form },
    onProgress, "Publish failed",
  );
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

// ── Profile ──────────────────────────────────

/** Identity, plan/quota, and work history in one call. Scoped server-side to
 *  the session's own user — no id is sent from here. */
export async function getProfile(): Promise<import("../types").Profile> {
  const res = await fetch(`${API_BASE}/api/profile`, { headers: authHeaders() });
  if (!res.ok) await parseError(res, "Could not load your profile");
  return res.json();
}

// ── Dashboard ────────────────────────────────

/** Totals, quota, the unified activity feed, and the 30-day series — one call.
 *  Same scoping as the profile: the server reads the session's own user_id. */
export async function getDashboard(): Promise<import("../types").Dashboard> {
  const res = await fetch(`${API_BASE}/api/dashboard`, { headers: authHeaders() });
  if (!res.ok) await parseError(res, "Could not load your dashboard");
  return res.json();
}

// ── Settings ─────────────────────────────────

export async function getSettings(): Promise<import("../types").UserSettings> {
  const res = await fetch(`${API_BASE}/api/settings`, { headers: authHeaders() });
  if (!res.ok) await parseError(res, "Could not load your settings");
  return res.json();
}

/** Patch form defaults. Omitted fields keep their current server-side value. */
export async function saveSettings(
  patch: Partial<import("../types").UserSettings>
): Promise<import("../types").UserSettings> {
  const res = await fetch(`${API_BASE}/api/settings`, {
    method: "PUT",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(patch),
  });
  if (!res.ok) await parseError(res, "Could not save your settings");
  return res.json();
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/auth/change-password`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  if (!res.ok) await parseError(res, "Could not change your password");
  return (await res.json()).message || "Password updated.";
}

/** Revoke every session, including this one — so drop the local token too. */
export async function logoutEverywhere(): Promise<string> {
  const res = await fetch(`${API_BASE}/api/auth/logout-all`, {
    method: "POST", headers: authHeaders(),
  });
  if (!res.ok) await parseError(res, "Could not sign out your other sessions");
  const data = await res.json();
  setToken("");
  broadcastLogout();
  return data.message || "";
}

/** Delete the account and all its history. `confirm` must echo the account's
 *  own email (or GitHub login) back — the server re-checks it. */
export async function deleteAccount(confirm: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/account`, {
    method: "DELETE",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ confirm }),
  });
  if (!res.ok) await parseError(res, "Could not delete your account");
  setToken("");
  broadcastLogout();
}

// ── Admin console ────────────────────────────
//
// Everything below is served behind the server's admin allowlist
// (backend/services/admin.py). A non-admin session gets 404 from all of it —
// deliberately, so the console doesn't announce itself to accounts that can't
// use it. That means `is_admin` on the User is the only reason to *render* these
// screens, and never the reason they're allowed to work.

export interface AdminUser {
  id: string;
  email?: string | null;
  name?: string | null;
  github_login?: string | null;
  avatar_url?: string | null;
  created_at: number;
  plan: string;
  plan_expires_at?: number | null;
  email_verified: boolean;
  suspended: boolean;
  suspended_at?: number | null;
  suspended_reason?: string | null;
  is_admin: boolean;
  generations: number;
  scans: number;
  usage_this_period: number;
}

export interface AdminUserList {
  users: AdminUser[];
  /** Rows matching the search, not the page — the pager needs the whole count. */
  total: number;
  limit: number;
  offset: number;
}

export interface AdminUserDetail {
  user: AdminUser;
  quota: QuotaInfo;
  totals: Record<string, number>;
  active_sessions: number;
  recent_generations: any[];
  recent_scans: any[];
  published_repos: any[];
}

export interface AdminOverview {
  totals: Record<string, number>;
  series: { date: string; generate: number; publish: number; scan: number; signups: number }[];
  frameworks: { name: string; count: number }[];
  languages: { name: string; count: number }[];
  recent_failures: any[];
}

export interface AdminKeyHealth {
  provider: string;
  /** The .env variable this key came from — never the key itself. */
  env_var: string;
  index: number;
  available: boolean;
  /** Out of credit or past a daily cap: waiting will NOT clear this. */
  hard_blocked: boolean;
  cooldown_seconds_left: number;
  call_count: number;
  error_count: number;
  last_used?: number | null;
}

export interface AdminSystem {
  app_env: string;
  uptime_seconds: number;
  jobs_stored: number;
  providers: { name: string; total_keys: number; available_keys: number; total_calls: number; healthy: boolean }[];
  keys: AdminKeyHealth[];
  call_log: any[];
  config: Record<string, boolean | number>;
}

async function adminGet<T>(path: string, fallback: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { headers: authHeaders() });
  if (!res.ok) await parseError(res, fallback);
  return res.json();
}

async function adminPost<T>(path: string, body: unknown, fallback: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (!res.ok) await parseError(res, fallback);
  return res.json();
}

/** Columns the server will sort on. Mirrors store._USER_SORTS — anything else
 *  is ignored server-side and falls back to newest-first, so a stale client
 *  degrades to the default rather than erroring. */
export type AdminUserSort =
  | "created_at" | "email" | "name" | "plan" | "suspended" | "email_verified";

export function fetchAdminUsers(
  q = "", limit = 25, offset = 0,
  sort: AdminUserSort = "created_at", direction: "asc" | "desc" = "desc",
): Promise<AdminUserList> {
  const params = new URLSearchParams({
    q, limit: String(limit), offset: String(offset), sort, direction,
  });
  return adminGet(`/api/admin/users?${params}`, "Could not load users");
}

export function fetchAdminUser(userId: string): Promise<AdminUserDetail> {
  return adminGet(`/api/admin/users/${encodeURIComponent(userId)}`, "Could not load that user");
}

export function fetchAdminOverview(): Promise<AdminOverview> {
  return adminGet("/api/admin/overview", "Could not load the overview");
}

export function fetchAdminSystem(): Promise<AdminSystem> {
  return adminGet("/api/admin/system", "Could not load system status");
}

export function adminSuspendUser(userId: string, suspended: boolean, reason = ""): Promise<AdminUser> {
  return adminPost(`/api/admin/users/${encodeURIComponent(userId)}/suspend`,
    { suspended, reason }, "Could not update that account");
}

/** Grant or revoke a plan by hand. `expiresInDays` omitted means it never lapses. */
export function adminSetPlan(userId: string, plan: string, expiresInDays?: number | null, reason = ""): Promise<AdminUser> {
  return adminPost(`/api/admin/users/${encodeURIComponent(userId)}/plan`,
    { plan, expires_in_days: expiresInDays ?? null, reason }, "Could not change that plan");
}

export function adminSetUsage(userId: string, count: number): Promise<AdminUser> {
  return adminPost(`/api/admin/users/${encodeURIComponent(userId)}/usage`,
    { count }, "Could not adjust usage");
}

export function adminLogoutUser(userId: string): Promise<{ message: string }> {
  return adminPost(`/api/admin/users/${encodeURIComponent(userId)}/logout-all`,
    {}, "Could not sign that user out");
}

/** Irreversible. `confirm` must echo the target's own email — the server
 *  re-checks it, exactly as the self-serve delete does. */
export async function adminDeleteUser(userId: string, confirm: string): Promise<string> {
  const params = new URLSearchParams({ confirm });
  const res = await fetch(
    `${API_BASE}/api/admin/users/${encodeURIComponent(userId)}?${params}`,
    { method: "DELETE", headers: authHeaders() },
  );
  if (!res.ok) await parseError(res, "Could not delete that account");
  return (await res.json()).message || "";
}
