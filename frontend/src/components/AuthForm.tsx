import { useState, useEffect } from "react";
import { Link, useNavigate, useLocation } from "react-router-dom";
import { Mail, Lock, User as UserIcon, Loader2, Github, ArrowRight, ShieldCheck, MailCheck } from "lucide-react";
import { useAuth } from "../context/AuthContext";
import { getAuthConfig, githubLoginUrl, resendVerification, type LoginResult } from "../utils/api";
import Page from "./Page";

const inputClass =
  "w-full pl-10 pr-4 py-3 rounded-xl bg-black/30 border border-grey-700 text-white placeholder:text-grey-400 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm";

/** Which panel the form is showing. Logging in is no longer one step: the
 *  password can be right and still not be enough, so the server's answer
 *  decides what comes next rather than the form assuming success. */
type Stage = "credentials" | "otp" | "verify-sent";

export default function AuthForm({ mode }: { mode: "login" | "signup" }) {
  const isSignup = mode === "signup";
  const { login, signup, completeOtp } = useAuth();
  const navigate = useNavigate();
  const location = useLocation() as any;
  // Where to land once authenticated: back to whatever the guard bounced them
  // off, else the dashboard — the signed-in home.
  const dest = location.state?.from || "/dashboard";

  const [stage, setStage] = useState<Stage>("credentials");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challengeId, setChallengeId] = useState("");
  const [emailHint, setEmailHint] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [githubOauth, setGithubOauth] = useState(false);
  const [resetEnabled, setResetEnabled] = useState(true);

  useEffect(() => {
    getAuthConfig()
      .then((c) => {
        setGithubOauth(c.github_oauth_enabled);
        setResetEnabled(c.password_reset_enabled);
      })
      .catch(() => {});
  }, []);

  /** Route on the server's verdict — the three cases are the LoginResult union. */
  const handleResult = (result: LoginResult) => {
    setEmailHint(result.email_hint || "");
    setNotice(result.message || "");
    if (result.status === "ok") {
      navigate(dest, { replace: true });
    } else if (result.status === "otp_required") {
      setChallengeId(result.challenge_id || "");
      setCode("");
      setStage("otp");
    } else {
      setStage("verify-sent");
    }
  };

  const submitCredentials = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      handleResult(
        isSignup
          ? await signup(email.trim(), password, name.trim() || undefined)
          : await login(email.trim(), password)
      );
    } catch (err: any) {
      setError(err.message || "Something went wrong");
    } finally { setBusy(false); }
  };

  const submitOtp = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await completeOtp(challengeId, code.trim());
      navigate(dest, { replace: true });
    } catch (err: any) {
      setError(err.message || "That code didn't work");
      setCode("");
    } finally { setBusy(false); }
  };

  const resend = async () => {
    setBusy(true); setError(null);
    try {
      setNotice(await resendVerification(email.trim()));
    } catch (err: any) {
      setError(err.message || "Could not resend the link");
    } finally { setBusy(false); }
  };

  const backToStart = () => {
    setStage("credentials"); setError(null); setNotice(""); setCode("");
  };

  // ── Panel: enter the emailed 6-digit code ──
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
          <button type="submit" disabled={busy || code.length !== 6}
            className="w-full flex items-center justify-center gap-2 py-4 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-[15px] leading-[22px]">
            {busy ? <><Loader2 size={16} className="animate-spin" /> Verifying…</> : <>Verify and log in <ArrowRight size={16} /></>}
          </button>
        </form>
        <p className="text-center text-xs text-grey-400 mt-4">
          The code expires shortly.{" "}
          <button onClick={backToStart} className="text-brand-300 hover:text-brand-200 font-medium">
            Start over
          </button>{" "}
          to get a new one.
        </p>
      </Shell>
    );
  }

  // ── Panel: a verification link is in their inbox ──
  if (stage === "verify-sent") {
    return (
      <Shell
        icon={<MailCheck size={22} className="text-emerald-300" />}
        title="Verify your email"
        subtitle={notice || `We sent a link to ${emailHint || "your inbox"}.`}
      >
        <p className="text-sm text-white/[72%] leading-relaxed">
          Click the link in that email to activate your account. It works once and
          expires in 24 hours — check your spam folder if it hasn't arrived.
        </p>
        {error && <div className="mt-3"><ErrorBox>{error}</ErrorBox></div>}
        <button onClick={resend} disabled={busy}
          className="w-full mt-5 flex items-center justify-center gap-2 py-3.5 rounded-xl bg-white/10 hover:bg-white/15 border border-white/25 text-white font-semibold transition-all text-sm disabled:opacity-40">
          {busy ? <><Loader2 size={15} className="animate-spin" /> Sending…</> : "Resend the link"}
        </button>
        <p className="text-center text-sm text-white/[72%] mt-5">
          <button onClick={backToStart} className="text-brand-300 hover:text-brand-200 font-medium">
            Back to {isSignup ? "sign up" : "log in"}
          </button>
        </p>
      </Shell>
    );
  }

  // ── Panel: email + password ──
  return (
    <Page className="flex items-center justify-center min-h-[70vh] pt-16">
      <div className="w-full max-w-md">
        <div className="text-center mb-10">
          <h1 className="font-display text-2xl font-bold">{isSignup ? "Create your account" : "Welcome back"}</h1>
          <p className="text-white/[72%] text-sm mt-2">
            {isSignup ? "Start generating tests in seconds — it's free." : "Log in to generate, publish, and scan."}
          </p>
        </div>

        <div className="glass rounded-3xl p-9 shadow-card space-y-6">
          {githubOauth && (
            <>
              <a href={githubLoginUrl()} className="w-full flex items-center justify-center gap-2.5 py-4 rounded-xl bg-white/10 hover:bg-white/15 border border-white/25 text-white font-semibold transition-all text-[15px] leading-[22px] shadow-card">
                <Github size={17} /> Continue with GitHub
              </a>
              <div className="flex items-center gap-3 text-xs text-grey-400">
                <div className="h-px flex-1 bg-white/10" /> or {isSignup ? "sign up" : "log in"} with email <div className="h-px flex-1 bg-white/10" />
              </div>
            </>
          )}

          <form onSubmit={submitCredentials} className="space-y-3">
            {isSignup && (
              <div className="relative">
                <UserIcon size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-400" />
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name (optional)" className={inputClass} />
              </div>
            )}
            <div className="relative">
              <Mail size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-400" />
              <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@email.com" className={inputClass} />
            </div>
            <div className="relative">
              <Lock size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-400" />
              <input type="password" required value={password} onChange={(e) => setPassword(e.target.value)}
                autoComplete={isSignup ? "new-password" : "current-password"}
                placeholder={isSignup ? "Create a password (min 8 chars)" : "Your password"} className={inputClass} />
            </div>

            {!isSignup && resetEnabled && (
              <div className="text-right">
                <Link to="/forgot-password" className="text-xs text-brand-300 hover:text-brand-200 font-medium">
                  Forgot your password?
                </Link>
              </div>
            )}

            {error && <ErrorBox>{error}</ErrorBox>}

            <button type="submit" disabled={busy || !email || !password}
              className="w-full flex items-center justify-center gap-2 py-4 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-[15px] leading-[22px]">
              {busy ? <><Loader2 size={16} className="animate-spin" /> {isSignup ? "Creating account..." : "Logging in..."}</>
                : <>{isSignup ? "Create account" : "Log in"} <ArrowRight size={16} /></>}
            </button>
          </form>
        </div>

        <p className="text-center text-sm text-white/[72%] mt-5">
          {isSignup ? (
            <>Already have an account? <Link to="/login" className="text-brand-300 hover:text-brand-200 font-medium">Log in</Link></>
          ) : (
            <>New here? <Link to="/signup" className="text-brand-300 hover:text-brand-200 font-medium">Create an account</Link></>
          )}
        </p>
      </div>
    </Page>
  );
}

function ErrorBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-4 py-2.5 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300">
      {children}
    </div>
  );
}

/** Card used by the follow-up panels (OTP, verification-sent). */
function Shell({ icon, title, subtitle, children }: {
  icon: React.ReactNode; title: string; subtitle?: string; children: React.ReactNode;
}) {
  return (
    <Page className="flex items-center justify-center min-h-[70vh] pt-16">
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
    </Page>
  );
}
