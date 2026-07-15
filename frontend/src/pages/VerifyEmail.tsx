import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { Loader2, AlertTriangle, CheckCircle2 } from "lucide-react";
import { useAuth } from "../context/AuthContext";
import * as api from "../utils/api";
import Page from "../components/Page";

/** Landing page for the link in the verification email (/verify-email?token=…).
 *
 *  Redeeming the token logs the user straight in — clicking a link from their
 *  inbox proves the same thing a code emailed to it would. */
export default function VerifyEmail() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { refresh } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  // The token is single-use, so a double-invoke (React StrictMode mounts twice
  // in dev) would spend it on the first call and fail the second.
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const token = params.get("token");
    if (!token) { setError("This link is missing its verification token."); return; }
    api.verifyEmail(token)
      .then(async () => { await refresh(); setDone(true); })
      .catch((e) => setError(e.message || "This verification link is invalid or has expired."));
  }, []);

  return (
    <Page className="flex items-center justify-center min-h-[70vh]">
      <div className="glass rounded-3xl p-8 text-center max-w-sm">
        {error ? (
          <>
            <AlertTriangle size={28} className="text-rose-400 mx-auto mb-3" />
            <p className="text-white/80 text-sm mb-1">{error}</p>
            <p className="text-grey-400 text-xs mb-4">
              Links expire after 24 hours and only work once. Log in to get a fresh one.
            </p>
            <Link to="/login" className="inline-block px-5 py-2.5 rounded-xl btn-primary text-sm font-semibold">
              Back to login
            </Link>
          </>
        ) : done ? (
          <>
            <CheckCircle2 size={28} className="text-emerald-400 mx-auto mb-3" />
            <h1 className="font-display text-lg font-bold mb-1">Email verified</h1>
            <p className="text-white/80 text-sm mb-4">You're all set — your account is active.</p>
            <button onClick={() => navigate("/dashboard", { replace: true })}
              className="px-5 py-2.5 rounded-xl btn-primary text-sm font-semibold">
              Go to your dashboard
            </button>
          </>
        ) : (
          <>
            <Loader2 size={28} className="text-brand-300 mx-auto mb-3 animate-spin" />
            <p className="text-grey-300 text-sm">Verifying your email…</p>
          </>
        )}
      </div>
    </Page>
  );
}
