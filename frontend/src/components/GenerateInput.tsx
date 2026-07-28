import { useState, useEffect } from "react";
import { Github, Globe, ArrowRight, Loader2, ShieldCheck, Lock } from "lucide-react";
import { getAuthConfig, githubLoginUrl, type SiteLogin } from "../utils/api";
import { useAuth } from "../context/AuthContext";

interface Props {
  /** Both URLs are required; the hosted URL is the site the AI writes tests from,
   *  the repo URL is used to confirm access and publish the suite back.
   *  `siteLogin` is optional and only set when the site needs signing into. */
  onContinue: (input: { repoUrl: string; hostedUrl: string; siteLogin: SiteLogin | null }) => void;
  loading: boolean;
  error: string | null;
}

export default function GenerateInput({ onContinue, loading, error }: Props) {
  const { user, refresh } = useAuth();
  const githubConnected = Boolean(user?.github_connected);
  const [repoUrl, setRepoUrl] = useState("");
  const [hostedUrl, setHostedUrl] = useState("");
  const [dismissed, setDismissed] = useState(false);
  // Optional site sign-in. Collapsed by default: a public site needs none, and
  // asking every user for credentials up front would be the wrong default.
  const [needsLogin, setNeedsLogin] = useState(false);
  const [loginUrl, setLoginUrl] = useState("");
  const [loginUser, setLoginUser] = useState("");
  const [loginPass, setLoginPass] = useState("");

  // Only /api/auth/me knows the session-derived github_connected flag, so refresh
  // it on mount — a password login's response can't carry it.
  useEffect(() => { getAuthConfig().catch(() => {}); refresh().catch(() => {}); }, []);
  useEffect(() => { if (error) setDismissed(false); }, [error]);

  // A half-filled login is worse than none: it would fail the sign-in and stop
  // the run, so both fields are required once the box is ticked.
  const loginReady = !needsLogin || (loginUser.trim() && loginPass);
  const ready = repoUrl.trim() && hostedUrl.trim() && loginReady;
  const submit = () => {
    if (!ready) return;
    onContinue({
      repoUrl: repoUrl.trim(),
      hostedUrl: hostedUrl.trim(),
      siteLogin: needsLogin
        ? { url: loginUrl.trim(), username: loginUser.trim(), password: loginPass }
        : null,
    });
  };

  // Hard gate: generation is crawl-first and publishes back to GitHub, so a
  // connected account is required before anything else is shown.
  if (!githubConnected) {
    return (
      <div className="w-full max-w-xl mx-auto">
        <div className="glass rounded-3xl p-8 text-center space-y-4 shadow-card">
          <div className="w-14 h-14 mx-auto rounded-2xl bg-white/5 flex items-center justify-center">
            <Github size={26} className="text-white/80" />
          </div>
          <div>
            <h3 className="text-lg font-semibold text-white">Connect GitHub to generate</h3>
            <p className="text-sm text-grey-400 mt-1.5 max-w-sm mx-auto">
              Testra reads your deployed site to write real tests and publishes the
              suite back to your repo. Connect your GitHub account to continue.
            </p>
          </div>
          <a
            href={githubLoginUrl("/generate")}
            className="inline-flex items-center justify-center gap-2 py-3 px-5 rounded-xl btn-primary font-semibold text-sm"
          >
            <Github size={16} /> Connect GitHub
          </a>
          <p className="flex items-center justify-center gap-1.5 text-xs text-grey-500">
            <ShieldCheck size={12} /> Only repo access — nothing is executed on your site.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="w-full max-w-xl mx-auto">
      <div className="glass rounded-3xl p-5 shadow-card space-y-4">
        <p className="flex items-center gap-1.5 text-xs text-emerald-300/80">
          <Github size={12} /> Connected as {user?.github_login}.
        </p>

        {/* Repo URL */}
        <div>
          <label htmlFor="gi-repo-url" className="block text-sm font-semibold text-white mb-2">GitHub repository</label>
          <div className="relative">
            <Github size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-500" />
            <input
              id="gi-repo-url" type="url"
              value={repoUrl}
              onChange={(e) => { setRepoUrl(e.target.value); setDismissed(true); }}
              placeholder="https://github.com/owner/repo"
              className="w-full pl-10 pr-4 py-3.5 rounded-xl bg-black/30 border border-grey-700 text-white placeholder:text-grey-400 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm"
            />
          </div>
        </div>

        {/* Hosted URL */}
        <div>
          <label htmlFor="gi-hosted-url" className="block text-sm font-semibold text-white mb-2">Hosted website URL</label>
          <div className="relative">
            <Globe size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-500" />
            <input
              id="gi-hosted-url" type="url"
              value={hostedUrl}
              onChange={(e) => { setHostedUrl(e.target.value); setDismissed(true); }}
              onKeyDown={(e) => e.key === "Enter" && submit()}
              placeholder="https://your-app.com"
              className="w-full pl-10 pr-4 py-3.5 rounded-xl bg-black/30 border border-grey-700 text-white placeholder:text-grey-400 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm font-mono"
            />
          </div>
          <p className="text-xs text-grey-500 mt-1.5">
            The deployed, publicly-reachable site. Testra renders it and writes tests
            from its real elements — nothing is executed, we only read the page.
          </p>
        </div>

        {/* Optional sign-in. Without it an auth-guarded app shows the crawler
            nothing but its login screen, and the suite can only test that. */}
        <div className="rounded-xl border border-grey-700/70 p-4 space-y-3">
          <label className="flex items-start gap-2.5 cursor-pointer">
            <input
              type="checkbox"
              checked={needsLogin}
              onChange={(e) => { setNeedsLogin(e.target.checked); setDismissed(true); }}
              className="mt-0.5 accent-brand-500"
            />
            <span>
              <span className="flex items-center gap-1.5 text-sm font-semibold text-white">
                <Lock size={13} /> This site needs a login
              </span>
              <span className="block text-xs text-grey-500 mt-1">
                We'll sign in first so the crawl can reach the pages behind it —
                otherwise the tests can only cover your sign-in screen.
              </span>
            </span>
          </label>

          {needsLogin && (
            <div className="space-y-2.5 pl-6">
              <input
                aria-label="Sign-in page URL" type="text" value={loginUrl}
                onChange={(e) => { setLoginUrl(e.target.value); setDismissed(true); }}
                placeholder="Sign-in page (e.g. /login) — optional"
                className="w-full px-3.5 py-2.5 rounded-lg bg-black/30 border border-grey-700 text-white placeholder:text-grey-500 focus:outline-none focus:border-brand-500/70 text-sm font-mono"
              />
              <div className="grid grid-cols-2 gap-2.5">
                <input
                  aria-label="Site username" type="text" value={loginUser} autoComplete="off"
                  onChange={(e) => { setLoginUser(e.target.value); setDismissed(true); }}
                  placeholder="Username or email"
                  className="w-full px-3.5 py-2.5 rounded-lg bg-black/30 border border-grey-700 text-white placeholder:text-grey-500 focus:outline-none focus:border-brand-500/70 text-sm"
                />
                <input
                  aria-label="Site password" type="password" value={loginPass} autoComplete="new-password"
                  onChange={(e) => { setLoginPass(e.target.value); setDismissed(true); }}
                  placeholder="Password"
                  className="w-full px-3.5 py-2.5 rounded-lg bg-black/30 border border-grey-700 text-white placeholder:text-grey-500 focus:outline-none focus:border-brand-500/70 text-sm"
                />
              </div>
              <p className="flex items-center gap-1.5 text-xs text-grey-500">
                <ShieldCheck size={12} />
                Used for this crawl only — never saved, and dropped before the job is stored.
                Use a test account.
              </p>
            </div>
          )}
        </div>

        {error && !dismissed && (
          <div className="px-4 py-3 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300">{error}</div>
        )}

        <button
          onClick={submit}
          disabled={loading || !ready}
          className="w-full flex items-center justify-center gap-2 py-3.5 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-sm"
        >
          {loading ? (<><Loader2 size={16} className="animate-spin" /> Working…</>)
            : (<>Continue <ArrowRight size={16} /></>)}
        </button>
      </div>
    </div>
  );
}
