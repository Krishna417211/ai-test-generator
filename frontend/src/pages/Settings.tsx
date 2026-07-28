import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate, useLocation, Link } from "react-router-dom";
import {
  Settings as SettingsIcon, Loader2, Check, AlertCircle, Github, LogOut,
  Trash2, KeyRound, SlidersHorizontal, Gauge, ShieldAlert, Link2,
  Terminal as TerminalIcon, Copy,
} from "lucide-react";
import Page from "../components/Page";
import PageHeader from "../components/PageHeader";
import { useAuth } from "../context/AuthContext";
import {
  getSettings, saveSettings, changePassword, logoutEverywhere, deleteAccount,
  getProfile, startCheckout, CheckoutUnavailableError, deleteRepo,
  getAuthConfig, githubLoginUrl,
} from "../utils/api";
import { pluralize } from "../utils/format";
import type { UserSettings, Profile, PlanCatalogueEntry } from "../types";

const FRAMEWORKS = [
  { value: "playwright", label: "Playwright" },
  { value: "cypress", label: "Cypress" },
  { value: "selenium", label: "Selenium" },
];
const LANGUAGES = [
  { value: "typescript", label: "TypeScript" },
  { value: "javascript", label: "JavaScript" },
  { value: "python", label: "Python" },
  { value: "java", label: "Java" },
];

function Section({ icon: Icon, title, description, children, danger = false, id, highlight = false }: {
  icon: any; title: string; description: string;
  children: React.ReactNode; danger?: boolean; id?: string; highlight?: boolean;
}) {
  return (
    <section id={id} className={`rounded-2xl border p-5 sm:p-6 transition-colors ${
      danger ? "border-rose-500/30 bg-rose-500/[0.04]"
      // Arriving from a "connect it in Settings" link lands on a page of five
      // near-identical cards; without this the user has to read all of them to
      // find the one they were sent for.
      : highlight ? "border-brand-400/50 bg-brand-400/[0.06]"
      : "border-grey-700 bg-white/[0.03]"}`}>
      <div className="flex items-center gap-2 mb-1">
        <Icon size={14} className={danger ? "text-rose-400" : "text-grey-400"} />
        <h2 className={`text-xs font-semibold uppercase tracking-wider ${
          danger ? "text-rose-400" : "text-grey-400"}`}>
          {title}
        </h2>
      </div>
      <p className="text-xs text-grey-500 mb-5 leading-relaxed">{description}</p>
      {children}
    </section>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-[13px] font-medium text-grey-200 mb-1.5">{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-grey-500 mt-1.5">{hint}</span>}
    </label>
  );
}

const inputCls =
  "w-full rounded-xl border border-grey-700 bg-white/[0.03] px-3.5 py-2.5 text-sm text-white " +
  "placeholder:text-grey-500 focus:border-grey-500 focus:outline-none transition-colors";

/** An inline result line. Success and failure differ by icon + wording, not
 *  colour alone. */
function Notice({ kind, children }: { kind: "ok" | "error"; children: React.ReactNode }) {
  const ok = kind === "ok";
  return (
    <div role={ok ? "status" : "alert"}
         className={`mt-4 flex items-start gap-2 rounded-xl border p-3 text-xs leading-relaxed ${
           ok ? "border-brand-400/30 bg-brand-400/10 text-brand-200"
              : "border-rose-500/30 bg-rose-500/10 text-rose-300"}`}>
      {ok ? <Check size={13} className="mt-0.5 shrink-0" />
          : <AlertCircle size={13} className="mt-0.5 shrink-0" />}
      <span>{children}</span>
    </div>
  );
}

// ── Generation defaults ──────────────────────

function DefaultsSection() {
  const [values, setValues] = useState<UserSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    getSettings().then(setValues).catch((e) =>
      setMsg({ kind: "error", text: e.message || "Could not load your settings." }));
  }, []);

  const save = async () => {
    if (!values) return;
    setBusy(true); setMsg(null);
    try {
      setValues(await saveSettings(values));
      setMsg({ kind: "ok", text: "Defaults saved — new Generate and Publish forms will start here." });
    } catch (e: any) {
      setMsg({ kind: "error", text: e.message || "Could not save your settings." });
    } finally { setBusy(false); }
  };

  return (
    <Section
      icon={SlidersHorizontal}
      title="Generation defaults"
      description="What the Generate and Publish forms start with, so you're not re-picking the same options every time."
    >
      {!values ? (
        <div className="flex items-center gap-2 text-xs text-grey-500 py-2">
          <Loader2 size={13} className="animate-spin" /> Loading…
        </div>
      ) : (
        <>
          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="Framework">
              <select className={inputCls} value={values.framework}
                      onChange={(e) => setValues({ ...values, framework: e.target.value })}>
                {FRAMEWORKS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
              </select>
            </Field>
            <Field label="Language">
              <select className={inputCls} value={values.language}
                      onChange={(e) => setValues({ ...values, language: e.target.value })}>
                {LANGUAGES.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
              </select>
            </Field>
          </div>
          <div className="mt-4">
            <Field label="Base URL" hint="Where generated tests point by default. Must start with http:// or https://">
              <input className={inputCls} value={values.base_url} inputMode="url"
                     onChange={(e) => setValues({ ...values, base_url: e.target.value })}
                     placeholder="http://localhost:3000" />
            </Field>
          </div>
          <button onClick={save} disabled={busy}
                  className="btn-primary mt-5 inline-flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold disabled:opacity-40">
            {busy ? <><Loader2 size={14} className="animate-spin" /> Saving…</> : "Save defaults"}
          </button>
          {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
        </>
      )}
    </Section>
  );
}

// ── Account ──────────────────────────────────

function AccountSection() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  // A GitHub-only account has no password, so there is nothing here to change —
  // say so rather than showing a form the server will refuse.
  const hasPassword = !!user?.email && !(user?.has_github && !user?.email);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setMsg(null);
    try {
      setMsg({ kind: "ok", text: await changePassword(current, next) });
      setCurrent(""); setNext("");
    } catch (err: any) {
      setMsg({ kind: "error", text: err.message || "Could not change your password." });
    } finally { setBusy(false); }
  };

  const signOutEverywhere = async () => {
    try {
      await logoutEverywhere();
    } catch { /* the token is dropped locally either way */ }
    await logout().catch(() => {});
    navigate("/login", { replace: true });
  };

  return (
    <Section icon={KeyRound} title="Account"
             description="Who you're signed in as, and how you get back in.">
      <div className="rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-3 mb-5">
        <div className="flex items-center gap-3">
          {user?.avatar_url ? (
            <img src={user.avatar_url} alt="" className="w-10 h-10 rounded-xl border border-grey-700" />
          ) : (
            <span className="w-10 h-10 rounded-xl bg-brand-gradient flex items-center justify-center text-sm font-bold text-ink-950">
              {(user?.name || user?.email || "?").charAt(0).toUpperCase()}
            </span>
          )}
          <div className="min-w-0">
            <div className="text-sm font-medium text-white truncate">{user?.name}</div>
            <div className="text-xs text-grey-400 truncate">{user?.email || "No email on file"}</div>
          </div>
          {user?.has_github && (
            <span className="ml-auto flex items-center gap-1.5 text-[11px] text-grey-400 shrink-0">
              <Github size={12} /> @{user.github_login}
            </span>
          )}
        </div>
      </div>

      {hasPassword ? (
        <form onSubmit={submit} className="grid sm:grid-cols-2 gap-4">
          <Field label="Current password">
            <input type="password" className={inputCls} value={current} required
                   autoComplete="current-password"
                   onChange={(e) => setCurrent(e.target.value)} />
          </Field>
          <Field label="New password" hint="At least 8 characters.">
            <input type="password" className={inputCls} value={next} required minLength={8}
                   autoComplete="new-password"
                   onChange={(e) => setNext(e.target.value)} />
          </Field>
          <div className="sm:col-span-2">
            <button type="submit" disabled={busy || !current || !next}
                    className="btn-primary inline-flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold disabled:opacity-40">
              {busy ? <><Loader2 size={14} className="animate-spin" /> Updating…</> : "Change password"}
            </button>
          </div>
        </form>
      ) : (
        <p className="text-xs text-grey-500 leading-relaxed">
          This account signs in with GitHub, so there's no password to change. Manage
          access from your GitHub account settings.
        </p>
      )}
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}

      <div className="mt-5 pt-5 border-t border-grey-700">
        <button onClick={signOutEverywhere}
                className="inline-flex items-center gap-2 rounded-xl btn-ghost px-4 py-2.5 text-sm text-grey-200">
          <LogOut size={14} /> Sign out everywhere
        </button>
        <p className="text-[11px] text-grey-500 mt-2">
          Ends every session on every device, including this one.
        </p>
      </div>
    </Section>
  );
}

// ── Connected accounts ───────────────────────

/** Where GitHub gets connected — once, on the account — rather than as a step
 *  inside the publish flow.
 *
 *  The distinction the UI has to get right is `has_github` vs
 *  `github_connected` (see /api/auth/me). The first says the *account* carries a
 *  GitHub identity; the second says *this session* holds an access token and can
 *  therefore push. A GitHub user who later signs in with a password has the
 *  first and not the second, and would hit a 403 at publish time with nothing on
 *  screen explaining why. So the button keys off `github_connected` and the
 *  wording off `has_github`.
 */
function ConnectionsSection({ highlight }: { highlight: boolean }) {
  const { user, refresh } = useAuth();
  // Optimistic until the server answers: the fallback to this button is asking
  // for a pasted Personal Access Token, which is the thing connecting exists to
  // avoid — don't show it because a config fetch was slow.
  const [oauth, setOauth] = useState<"unknown" | "on" | "off">("unknown");
  const oauthEnabled = oauth !== "off";
  const connected = Boolean(user?.github_connected);

  useEffect(() => {
    getAuthConfig().then((c) => setOauth(c.github_oauth_enabled ? "on" : "off")).catch(() => {});
    // The return trip from OAuth lands here; re-ask so the card shows connected
    // without a manual reload. `user` in hand may predate the round-trip.
    refresh().catch(() => {});
  }, []);

  // Back here afterwards, on this card, with the hash that highlights it.
  const connectUrl = githubLoginUrl("/settings#github");

  return (
    <Section
      id="github"
      highlight={highlight}
      icon={Link2}
      title="Connected accounts"
      description="Connect GitHub once here, and publishing works without asking you for anything."
    >
      <div className="rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-3.5">
        <div className="flex flex-wrap items-center gap-3">
          <Github size={18} className={connected ? "text-brand-300" : "text-grey-400"} />
          <div className="min-w-0">
            <div className="text-sm font-medium text-white">GitHub</div>
            <div className="text-xs text-grey-400 truncate">
              {connected
                ? <>Connected as <span className="font-mono text-grey-200">@{user?.github_login}</span> — Testra can create repos and push on your behalf.</>
                : user?.has_github
                  // The identity is on the account but this session has no push
                  // token. Saying "not connected" flatly reads as "the connection
                  // I made last week vanished", so name what actually happened.
                  ? <>Linked to <span className="font-mono text-grey-200">@{user.github_login}</span>, but this sign-in didn't include push access.</>
                  : "Not connected."}
            </div>
          </div>
          {oauthEnabled && (
            <a href={connectUrl}
               className={`ml-auto inline-flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold shrink-0 ${
                 connected
                   ? "btn-ghost text-grey-200"
                   : "btn-primary"}`}>
              <Github size={14} />
              {connected ? "Reconnect" : user?.has_github ? "Re-authorise GitHub" : "Connect GitHub"}
            </a>
          )}
        </div>
      </div>

      {oauthEnabled ? (
        <p className="text-[11px] text-grey-500 mt-3 leading-relaxed">
          You approve it on GitHub — there's no token to paste, and we never see your
          password. {connected
            ? "Reconnect if a push starts failing: an access token can be revoked on GitHub's side at any time, and nothing tells us until it's used."
            : "Until it's connected, Publish has nothing to push with."}
        </p>
      ) : (
        <p className="text-[11px] text-amber-300/80 mt-3 leading-relaxed">
          GitHub sign-in isn't configured on this server, so there's nothing to connect
          here — Publish will ask for a Personal Access Token (repo scope) instead.
        </p>
      )}
    </Section>
  );
}

// ── Command line ─────────────────────────────

/** Where the CLI is discoverable at all.
 *
 *  Without this, `npx testra` exists and nobody using the web app ever learns
 *  it does — and the CLI is strictly better for the publish flow, because it
 *  runs inside the project: your .gitignore decides what uploads, and the
 *  credential scan happens before anything leaves your machine rather than after
 *  it arrives here.
 *
 *  Deliberately not a nav item. /cli is a landing page for approving a code, not
 *  somewhere you browse to, and it would sit oddly next to "Generate".
 */
function CliSection() {
  const [copied, setCopied] = useState(false);
  const command = "npx testra login && npx testra publish --with-ci";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked (insecure origin / permission) — the text is on screen */
    }
  };

  return (
    <Section icon={TerminalIcon} title="Command line"
             description="Publish from the directory your project lives in, without zipping anything.">
      <div className="rounded-xl bg-black/40 border border-grey-700 px-3.5 py-3 flex items-center gap-3">
        <code className="text-xs font-mono text-grey-200 truncate flex-1">{command}</code>
        <button
          onClick={copy}
          title="Copy"
          className="p-1.5 rounded-lg hover:bg-white/10 text-grey-500 hover:text-grey-300 transition-all shrink-0"
        >
          {copied ? <Check size={13} className="text-emerald-400" /> : <Copy size={13} />}
        </button>
      </div>
      <ul className="mt-3 space-y-1.5">
        {[
          "Your .gitignore decides what uploads — no hand-made ZIP.",
          "Credentials are found and held back before anything is sent.",
          "Runs in CI: set TESTRA_TOKEN from a secret.",
        ].map((line) => (
          <li key={line} className="text-xs text-grey-400 flex gap-2">
            <Check size={12} className="text-brand-300 mt-0.5 shrink-0" />
            <span>{line}</span>
          </li>
        ))}
      </ul>
      <p className="text-xs text-grey-500 mt-3 leading-relaxed">
        <code className="font-mono text-grey-400">testra login</code> shows a short
        code and sends you to{" "}
        <Link to="/cli" className="text-brand-300 hover:text-brand-200">this page</Link>{" "}
        to approve it — your password never passes through the terminal.
      </p>
    </Section>
  );
}

// ── Plan & billing ───────────────────────────

function PlanSection({ profile }: { profile: Profile }) {
  const [selected, setSelected] = useState<"monthly" | "yearly">("yearly");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const plan = profile.plan;
  const isPro = plan.plan !== "free";
  const usedPct = plan.limit ? Math.min(100, Math.round((plan.used / plan.limit) * 100)) : 0;

  const byId = (id: string) => plan.catalogue.find((p) => p.id === id);
  const chosen: PlanCatalogueEntry | undefined = byId(selected);

  const upgrade = async () => {
    setBusy(true); setError(null);
    try {
      window.location.href = await startCheckout(selected);
    } catch (err: any) {
      setError(err instanceof CheckoutUnavailableError
        ? "Checkout isn't live yet — we can't take payments through the app right now."
        : err.message || "Could not start checkout.");
      setBusy(false);
    }
  };

  return (
    <Section icon={Gauge} title="Plan & billing"
             description="Your current plan and how much of this month's quota you've used.">
      <div className="flex items-baseline justify-between mb-2 gap-2">
        <span className="text-sm text-grey-300">
          {isPro ? "Pro — unlimited generations" : `${plan.used} of ${plan.limit} generations used`}
        </span>
        <span className="text-xs text-grey-500">
          {isPro
            ? plan.expires_at ? `Renews ${new Date(plan.expires_at * 1000).toLocaleDateString()}` : "Active"
            : `Resets ${new Date(plan.resets_at * 1000).toLocaleDateString()}`}
        </span>
      </div>
      {!isPro && plan.limit !== null && (
        <div className="h-2 rounded-full bg-white/[0.06] overflow-hidden">
          <div className={`h-full rounded-full transition-all duration-700 ${
            usedPct >= 100 ? "bg-amber-400" : "bg-brand-gradient"}`}
               style={{ width: `${usedPct}%` }} />
        </div>
      )}

      {!isPro && (
        <>
          <div className="mt-5 grid sm:grid-cols-2 gap-2.5">
            {[byId("yearly"), byId("monthly")].filter(Boolean).map((p) => (
              <button key={p!.id} onClick={() => setSelected(p!.id)}
                      aria-pressed={selected === p!.id}
                      className={`flex items-center justify-between rounded-2xl border p-4 text-left transition ${
                        selected === p!.id
                          ? "border-grey-300 bg-white/[0.08] shadow-inner-hi"
                          : "border-grey-700 bg-white/[0.02] hover:border-grey-600"}`}>
                <span className="flex items-center gap-3 min-w-0">
                  <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
                    selected === p!.id ? "border-brand-400 bg-brand-400" : "border-grey-600"}`}>
                    {selected === p!.id && <Check size={10} className="text-ink-950" strokeWidth={3.5} />}
                  </span>
                  <span className="font-semibold capitalize text-sm truncate">{p!.id}</span>
                  {!!p!.savings_usd && (
                    <span className="rounded-full bg-brand-400/15 px-2 py-0.5 text-[11px] font-medium text-brand-200 shrink-0">
                      Save ${p!.savings_usd}
                    </span>
                  )}
                </span>
                <span className="font-display text-lg font-bold shrink-0">
                  ${p!.price_usd}
                  <span className="text-xs font-normal text-grey-400">
                    /{p!.interval === "year" ? "yr" : "mo"}
                  </span>
                </span>
              </button>
            ))}
          </div>

          {/* No payment provider is wired up yet — say so plainly rather than
              letting the button 503 on click. */}
          {!plan.checkout_available && (
            <Notice kind="error">
              Checkout isn't live yet — payments aren't connected to this app right now.
            </Notice>
          )}
          {error && <Notice kind="error">{error}</Notice>}

          <button onClick={upgrade} disabled={busy || !plan.checkout_available}
                  className="btn-primary mt-4 flex w-full items-center justify-center gap-2 rounded-xl py-3 font-semibold disabled:opacity-40 disabled:cursor-not-allowed">
            {busy ? <><Loader2 size={16} className="animate-spin" /> Starting checkout…</>
                  : <>Upgrade to Pro — ${chosen?.price_usd}{selected === "yearly" ? "/yr" : "/mo"}</>}
          </button>
        </>
      )}
      {isPro && (
        <p className="mt-3 text-[11px] text-grey-500">Thanks for supporting Testra.</p>
      )}
    </Section>
  );
}

// ── Danger zone ──────────────────────────────

function DangerSection({ profile, reload }: { profile: Profile; reload: () => void }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [repoBusy, setRepoBusy] = useState<string | null>(null);

  const expected = user?.email || user?.github_login || "";

  const removeRepo = async (fullName: string) => {
    setRepoBusy(fullName); setError(null);
    try {
      await deleteRepo(fullName);
      reload();
    } catch (e: any) {
      setError(e.message || `Could not delete ${fullName}.`);
    } finally { setRepoBusy(null); }
  };

  const wipe = async () => {
    setBusy(true); setError(null);
    try {
      await deleteAccount(confirm.trim());
      await logout().catch(() => {});
      navigate("/", { replace: true });
    } catch (e: any) {
      setError(e.message || "Could not delete your account.");
      setBusy(false);
    }
  };

  return (
    <Section icon={ShieldAlert} title="Danger zone" danger
             description="Irreversible. Read before you click.">
      <div className="mb-5">
        <div className="text-[13px] font-medium text-grey-200 mb-2">
          Repos published through Testra
        </div>
        {profile.repos.length === 0 ? (
          <p className="text-xs text-grey-500">Nothing published yet.</p>
        ) : (
          <div className="space-y-2">
            {profile.repos.map((r) => (
              <div key={r.full_name}
                   className="flex items-center justify-between gap-3 rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-2.5">
                <a href={r.repo_url} target="_blank" rel="noopener noreferrer"
                   className="text-xs font-mono text-grey-200 truncate hover:text-white transition-colors">
                  {r.full_name}
                </a>
                <button onClick={() => removeRepo(r.full_name)} disabled={repoBusy === r.full_name}
                        className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px] font-medium text-rose-300 hover:bg-rose-500/10 transition-colors disabled:opacity-40 shrink-0">
                  {repoBusy === r.full_name
                    ? <Loader2 size={11} className="animate-spin" />
                    : <Trash2 size={11} />}
                  Delete on GitHub
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="pt-5 border-t border-rose-500/20">
        <div className="text-[13px] font-medium text-grey-200 mb-1.5">Delete account</div>
        <p className="text-xs text-grey-500 leading-relaxed mb-4">
          Erases your account, {pluralize(profile.totals.generations, "generation")},{" "}
          {pluralize(profile.totals.scans, "scan")}, and your publish history. Repos
          already on GitHub are <strong className="text-grey-300">not</strong> touched —
          they're yours; delete them above first if you want them gone.
        </p>
        <Field label={`Type ${expected} to confirm`}>
          <input className={inputCls} value={confirm} autoComplete="off"
                 onChange={(e) => setConfirm(e.target.value)} placeholder={expected} />
        </Field>
        {error && <Notice kind="error">{error}</Notice>}
        <button onClick={wipe} disabled={busy || confirm.trim().toLowerCase() !== expected.toLowerCase()}
                className="mt-4 inline-flex items-center gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-2.5 text-sm font-semibold text-rose-300 transition-colors hover:bg-rose-500/20 disabled:opacity-40 disabled:cursor-not-allowed">
          {busy ? <><Loader2 size={14} className="animate-spin" /> Deleting…</>
                : <><Trash2 size={14} /> Delete my account</>}
        </button>
      </div>
    </Section>
  );
}

export default function Settings() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { hash } = useLocation();
  const wantsGithub = hash === "#github";
  const scrolled = useRef(false);

  // react-router doesn't restore hash targets itself, and the section isn't in
  // the DOM on the first paint anyway. Scroll once, after it mounts.
  useEffect(() => {
    if (!wantsGithub || scrolled.current) return;
    scrolled.current = true;
    requestAnimationFrame(() =>
      document.getElementById("github")?.scrollIntoView({ behavior: "smooth", block: "center" }));
  }, [wantsGithub]);

  const load = useCallback(async () => {
    try {
      setProfile(await getProfile());
      setError(null);
    } catch (e: any) {
      setError(e.message || "Could not load your settings.");
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <Page className="pb-8">
      <PageHeader icon={SettingsIcon} eyebrow="Your account" title="Settings"
                  subtitle="Defaults, credentials, plan, and the irreversible stuff." />
      <div className="space-y-5">
        <DefaultsSection />
        <AccountSection />
        <ConnectionsSection highlight={wantsGithub} />
        <CliSection />
        {error && (
          <div className="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-xs text-rose-300">
            {error}
          </div>
        )}
        {profile && <PlanSection profile={profile} />}
        {profile && <DangerSection profile={profile} reload={load} />}
      </div>
    </Page>
  );
}
