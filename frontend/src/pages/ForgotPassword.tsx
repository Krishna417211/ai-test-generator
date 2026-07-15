import { useState } from "react";
import { Link } from "react-router-dom";
import { Mail, Loader2, ArrowRight, MailCheck } from "lucide-react";
import * as api from "../utils/api";
import Page from "../components/Page";

/** Start a password reset. The server answers identically whether or not the
 *  address has an account, so this page must not imply it found one — the copy
 *  below deliberately says "if an account exists". */
export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      setMessage(await api.forgotPassword(email.trim()));
      setSent(true);
    } catch (err: any) {
      setError(err.message || "Could not start the reset");
    } finally { setBusy(false); }
  };

  return (
    <Page className="flex items-center justify-center min-h-[70vh] pt-16">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <div className="w-12 h-12 rounded-2xl bg-white/10 border border-white/15 flex items-center justify-center mx-auto mb-4">
            {sent ? <MailCheck size={22} className="text-emerald-300" /> : <Mail size={22} className="text-brand-300" />}
          </div>
          <h1 className="font-display text-2xl font-bold">
            {sent ? "Check your inbox" : "Reset your password"}
          </h1>
          <p className="text-white/[72%] text-sm mt-2">
            {sent
              ? message || "If an account exists for that address, a reset link is on its way."
              : "Enter your email and we'll send you a link to set a new password."}
          </p>
        </div>

        <div className="glass rounded-3xl p-9 shadow-card">
          {sent ? (
            <p className="text-sm text-white/[72%] leading-relaxed">
              The link works once and expires in an hour. If nothing arrives, check
              your spam folder — and note that accounts created with "Continue with
              GitHub" have no password to reset; use GitHub to log in instead.
            </p>
          ) : (
            <form onSubmit={submit} className="space-y-3">
              <div className="relative">
                <Mail size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-400" />
                <input type="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@email.com"
                  className="w-full pl-10 pr-4 py-3 rounded-xl bg-black/30 border border-grey-700 text-white placeholder:text-grey-400 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm" />
              </div>
              {error && (
                <div className="px-4 py-2.5 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300">{error}</div>
              )}
              <button type="submit" disabled={busy || !email}
                className="w-full flex items-center justify-center gap-2 py-4 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-[15px] leading-[22px]">
                {busy ? <><Loader2 size={16} className="animate-spin" /> Sending…</> : <>Send reset link <ArrowRight size={16} /></>}
              </button>
            </form>
          )}
        </div>

        <p className="text-center text-sm text-white/[72%] mt-5">
          Remembered it? <Link to="/login" className="text-brand-300 hover:text-brand-200 font-medium">Log in</Link>
        </p>
      </div>
    </Page>
  );
}
