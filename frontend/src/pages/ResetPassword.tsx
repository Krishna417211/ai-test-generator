import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Lock, Loader2, ArrowRight, AlertTriangle } from "lucide-react";
import { useAuth } from "../context/AuthContext";
import * as api from "../utils/api";
import Page from "../components/Page";

const inputClass =
  "w-full pl-10 pr-4 py-3 rounded-xl bg-black/30 border border-grey-700 text-white placeholder:text-grey-400 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm";

/** Landing page for the link in the reset email (/reset-password?token=…).
 *
 *  A successful reset signs every other session out server-side and logs this
 *  one in, so there's nowhere to send the user but into the app. */
export default function ResetPassword() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { refresh } = useAuth();
  const token = params.get("token") || "";

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mismatch = confirm.length > 0 && password !== confirm;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (password !== confirm) { setError("Those passwords don't match."); return; }
    setBusy(true); setError(null);
    try {
      await api.resetPassword(token, password);
      await refresh();
      navigate("/dashboard", { replace: true });
    } catch (err: any) {
      setError(err.message || "Could not reset your password");
    } finally { setBusy(false); }
  };

  if (!token) {
    return (
      <Page className="flex items-center justify-center min-h-[70vh]">
        <div className="glass rounded-3xl p-8 text-center max-w-sm">
          <AlertTriangle size={28} className="text-rose-400 mx-auto mb-3" />
          <p className="text-white/80 text-sm mb-4">This link is missing its reset token.</p>
          <Link to="/forgot-password" className="inline-block px-5 py-2.5 rounded-xl btn-primary text-sm font-semibold">
            Request a new link
          </Link>
        </div>
      </Page>
    );
  }

  return (
    <Page className="flex items-center justify-center min-h-[70vh] pt-16">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <div className="w-12 h-12 rounded-2xl bg-white/10 border border-white/15 flex items-center justify-center mx-auto mb-4">
            <Lock size={22} className="text-brand-300" />
          </div>
          <h1 className="font-display text-2xl font-bold">Choose a new password</h1>
          <p className="text-white/[72%] text-sm mt-2">
            This signs out anyone else using your account.
          </p>
        </div>

        <div className="glass rounded-3xl p-9 shadow-card">
          <form onSubmit={submit} className="space-y-3">
            <div className="relative">
              <Lock size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-400" />
              <input type="password" required autoFocus autoComplete="new-password"
                value={password} onChange={(e) => setPassword(e.target.value)}
                placeholder="New password (min 8 chars)" className={inputClass} />
            </div>
            <div className="relative">
              <Lock size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-400" />
              <input type="password" required autoComplete="new-password"
                value={confirm} onChange={(e) => setConfirm(e.target.value)}
                placeholder="Confirm new password" className={inputClass} />
            </div>

            {mismatch && (
              <p className="text-xs text-amber-300/90 px-1">Those passwords don't match yet.</p>
            )}
            {error && (
              <div className="px-4 py-2.5 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300">{error}</div>
            )}

            <button type="submit" disabled={busy || password.length < 8 || mismatch || !confirm}
              className="w-full flex items-center justify-center gap-2 py-4 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-[15px] leading-[22px]">
              {busy ? <><Loader2 size={16} className="animate-spin" /> Saving…</> : <>Set new password <ArrowRight size={16} /></>}
            </button>
          </form>
        </div>

        <p className="text-center text-sm text-white/[72%] mt-5">
          Link expired? <Link to="/forgot-password" className="text-brand-300 hover:text-brand-200 font-medium">Request a new one</Link>
        </p>
      </div>
    </Page>
  );
}
