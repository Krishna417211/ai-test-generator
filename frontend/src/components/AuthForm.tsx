import { useState, useEffect } from "react";
import { Link, useNavigate, useLocation } from "react-router-dom";
import { Mail, Lock, User as UserIcon, Loader2, Github, TestTube2, ArrowRight } from "lucide-react";
import { useAuth } from "../context/AuthContext";
import { getAuthConfig, githubLoginUrl } from "../utils/api";
import Page from "./Page";

export default function AuthForm({ mode }: { mode: "login" | "signup" }) {
  const isSignup = mode === "signup";
  const { login, signup } = useAuth();
  const navigate = useNavigate();
  const location = useLocation() as any;
  const dest = location.state?.from || "/generate";

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [oauth, setOauth] = useState(false);

  useEffect(() => { getAuthConfig().then((c) => setOauth(c.github_oauth_enabled)).catch(() => {}); }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      if (isSignup) await signup(email.trim(), password, name.trim() || undefined);
      else await login(email.trim(), password);
      navigate(dest, { replace: true });
    } catch (err: any) {
      setError(err.message || "Something went wrong");
    } finally { setBusy(false); }
  };

  return (
    <Page className="flex items-center justify-center min-h-[70vh]">
      <div className="w-full max-w-md">
        <div className="text-center mb-7">
          <div className="inline-flex items-center gap-2.5 mb-4">
            <span className="w-9 h-9 rounded-xl bg-brand-gradient flex items-center justify-center shadow-glow">
              <TestTube2 size={18} className="text-ink-950" />
            </span>
            <span className="font-display font-bold text-lg">Test<span className="text-gradient">ra</span></span>
          </div>
          <h1 className="font-display text-2xl font-bold">{isSignup ? "Create your account" : "Welcome back"}</h1>
          <p className="text-white/60 text-sm mt-1.5">
            {isSignup ? "Start generating tests in seconds — it's free." : "Log in to generate, publish, and scan."}
          </p>
        </div>

        <div className="glass rounded-3xl p-6 shadow-card space-y-4">
          {oauth && (
            <>
              <a href={githubLoginUrl()} className="w-full flex items-center justify-center gap-2.5 py-3 rounded-xl bg-black/40 hover:bg-black/60 border border-white/15 text-white font-semibold transition-all text-sm">
                <Github size={17} /> Continue with GitHub
              </a>
              <div className="flex items-center gap-3 text-xs text-white/40">
                <div className="h-px flex-1 bg-white/10" /> or {isSignup ? "sign up" : "log in"} with email <div className="h-px flex-1 bg-white/10" />
              </div>
            </>
          )}

          <form onSubmit={submit} className="space-y-3">
            {isSignup && (
              <div className="relative">
                <UserIcon size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-white/40" />
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name (optional)"
                  className="w-full pl-10 pr-4 py-3 rounded-xl bg-black/30 border border-white/10 text-white placeholder:text-white/45 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm" />
              </div>
            )}
            <div className="relative">
              <Mail size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-white/40" />
              <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@email.com"
                className="w-full pl-10 pr-4 py-3 rounded-xl bg-black/30 border border-white/10 text-white placeholder:text-white/45 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm" />
            </div>
            <div className="relative">
              <Lock size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-white/40" />
              <input type="password" required value={password} onChange={(e) => setPassword(e.target.value)}
                placeholder={isSignup ? "Create a password (min 8 chars)" : "Your password"}
                className="w-full pl-10 pr-4 py-3 rounded-xl bg-black/30 border border-white/10 text-white placeholder:text-white/45 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm" />
            </div>

            {error && <div className="px-4 py-2.5 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300">{error}</div>}

            <button type="submit" disabled={busy || !email || !password}
              className="w-full flex items-center justify-center gap-2 py-3.5 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-sm">
              {busy ? <><Loader2 size={16} className="animate-spin" /> {isSignup ? "Creating account..." : "Logging in..."}</>
                : <>{isSignup ? "Create account" : "Log in"} <ArrowRight size={16} /></>}
            </button>
          </form>
        </div>

        <p className="text-center text-sm text-white/60 mt-5">
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
