import { useState } from "react";
import { Mail, Lock, Loader2, ArrowRight, ShieldAlert, ShieldCheck, LogOut } from "lucide-react";
import { useAuth } from "../context/AuthContext";
import type { LoginResult } from "../utils/api";

const inputClass =
  "w-full pl-10 pr-4 py-3 rounded-xl bg-black/30 border border-grey-700 text-white placeholder:text-grey-400 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm";

/** Which panel is showing. Same three-way split as the main AuthForm, because
 *  it's the same server flow: a correct password is not, on its own, a session. */
type Stage = "credentials" | "otp";

/**
 * The gate on the standalone admin portal.
 *
 *  Posts to the ordinary /api/auth/login — there is no separate admin
 *  credential and no separate endpoint. That matters for three reasons:
 *  there's no second password store to leak, the emailed-OTP second factor
 *  applies here exactly as it does everywhere else, and the existing rate
 *  limits on that route already cover this form. A bespoke "admin login"
 *  endpoint would have had to re-earn all three.
 *
 *  Authenticating is therefore not the same as being let in: any account can
 *  log in through this form, and one that isn't on the server's ADMIN_EMAILS
 *  allowlist lands on the refusal panel below with a live session but no
 *  console. The allowlist, not this component, is the actual authority.
 */
export default function AdminLogin() {
  const { user, login, completeOtp, logout } = useAuth();

  const [stage, setStage] = useState<Stage>("credentials");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challengeId, setChallengeId] = useState("");
  const [emailHint, setEmailHint] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** Back to an empty sign-in form.
   *
   *  Needed because this component does not unmount between attempts — the gate
   *  above it renders the same instance for "logged out" and "logged in without
   *  the role", so signing out of a refused account would otherwise leave the
   *  stage on "otp" and redisplay the previous account's code.
   */
  const resetForm = () => {
    setStage("credentials");
    setEmail(""); setPassword(""); setCode(""); setChallengeId("");
    setEmailHint(""); setNotice(""); setError(null);
  };

  // Signed in, but not an admin. Reached either by logging in here with the
  // wrong account, or by arriving at /admin on an ordinary app session. Not an
  // error state — it just needs a way out that isn't "clear your cookies".
  if (user && !user.is_admin) {
    return (
      <Shell
        icon={<ShieldAlert size={22} className="text-amber-400" />}
        title="Not an admin account"
        subtitle={`You're signed in as ${user.email || user.name || "this account"}.`}
      >
        <p className="text-sm text-white/[72%] leading-relaxed">
          This account doesn't have admin access. Sign out and use an admin
          address, or ask whoever runs this instance to add yours.
        </p>
        <button
          onClick={async () => { await logout(); resetForm(); }}
          className="w-full mt-5 flex items-center justify-center gap-2 py-3.5 rounded-xl bg-white/10 hover:bg-white/15 border border-white/25 text-white font-semibold transition-all text-sm"
        >
          <LogOut size={15} /> Sign out and try another account
        </button>
      </Shell>
    );
  }

  const handleResult = (result: LoginResult) => {
    setEmailHint(result.email_hint || "");
    setNotice(result.message || "");
    if (result.status === "otp_required") {
      setChallengeId(result.challenge_id || "");
      setCode("");
      setStage("otp");
      return;
    }
    if (result.status === "verification_required") {
      // An admin address must be verified to count (services/auth.is_admin), so
      // this is a dead end here rather than something to walk them through.
      setError(result.message || "Confirm your email address before signing in.");
    }
    // "ok" needs nothing: useAuth set the user, and the gate above this
    // component re-renders straight into the console.
  };

  const submitCredentials = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      handleResult(await login(email.trim(), password));
    } catch (err: any) {
      setError(err.message || "Could not sign in.");
    } finally { setBusy(false); }
  };

  const submitOtp = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await completeOtp(challengeId, code.trim());
    } catch (err: any) {
      setError(err.message || "That code didn't work.");
      setCode("");
    } finally { setBusy(false); }
  };

  if (stage === "otp") {
    return (
      <Shell
        icon={<ShieldCheck size={22} className="text-brand-300" />}
        title="Check your email"
        subtitle={emailHint ? `We sent a 6-digit code to ${emailHint}.` : notice}
      >
        <form onSubmit={submitOtp} className="space-y-3">
          <input
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
            inputMode="numeric"
            autoComplete="one-time-code"
            autoFocus
            placeholder="000000"
            aria-label="6-digit login code"
            className="w-full px-4 py-4 rounded-xl bg-black/30 border border-grey-700 text-white text-center text-2xl tracking-[0.4em] placeholder:text-grey-600 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all"
          />
          {error && <ErrorBox>{error}</ErrorBox>}
          <button
            type="submit"
            disabled={busy || code.length !== 6}
            className="w-full flex items-center justify-center gap-2 py-4 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-[15px] leading-[22px]"
          >
            {busy
              ? <><Loader2 size={16} className="animate-spin" /> Verifying…</>
              : <>Verify and open console <ArrowRight size={16} /></>}
          </button>
        </form>
        <p className="text-center text-xs text-grey-400 mt-4">
          The code expires shortly.{" "}
          <button
            onClick={resetForm}
            className="text-brand-300 hover:text-brand-200 font-medium"
          >
            Start over
          </button>{" "}
          to get a new one.
        </p>
      </Shell>
    );
  }

  return (
    <Shell
      icon={<ShieldAlert size={22} className="text-amber-400" />}
      title="Admin sign-in"
      subtitle="This console manages every account on this instance."
    >
      <form onSubmit={submitCredentials} className="space-y-3">
        <div className="relative">
          <Mail size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-400" />
          <input
            type="email" required autoFocus value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="Admin email" autoComplete="username"
            className={inputClass}
          />
        </div>
        <div className="relative">
          <Lock size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-400" />
          <input
            type="password" required value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password" autoComplete="current-password"
            className={inputClass}
          />
        </div>

        {error && <ErrorBox>{error}</ErrorBox>}

        <button
          type="submit"
          disabled={busy || !email || !password}
          className="w-full flex items-center justify-center gap-2 py-4 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-[15px] leading-[22px]"
        >
          {busy
            ? <><Loader2 size={16} className="animate-spin" /> Signing in…</>
            : <>Sign in <ArrowRight size={16} /></>}
        </button>
      </form>
      {/* No "create an account" and no GitHub button: neither can make an admin
          (the allowlist does), so offering them here would only mislead. */}
      <p className="text-center text-xs text-grey-500 mt-5">
        Admin access is granted in the server's configuration, not from this page.
      </p>
    </Shell>
  );
}

function ErrorBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-4 py-2.5 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300">
      {children}
    </div>
  );
}

/** Standalone card. Deliberately not the app's <Page>: this screen sits outside
 *  the product shell, so it carries no NavBar, footer, or marketing chrome. */
function Shell({ icon, title, subtitle, children }: {
  icon: React.ReactNode; title: string; subtitle?: string; children: React.ReactNode;
}) {
  return (
    <main className="relative z-10 min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <div className="w-12 h-12 rounded-2xl bg-white/10 border border-white/15 flex items-center justify-center mx-auto mb-4">
            {icon}
          </div>
          <h1 className="font-display text-2xl font-bold">{title}</h1>
          {subtitle && <p className="text-white/[72%] text-sm mt-2">{subtitle}</p>}
        </div>
        <div className="glass rounded-3xl p-9 shadow-card">{children}</div>
      </div>
    </main>
  );
}
