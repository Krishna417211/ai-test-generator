import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Loader2, AlertTriangle } from "lucide-react";
import { useAuth } from "../context/AuthContext";
import Page from "../components/Page";

/** Reasons the callback needs to say something other than "sign-in failed".
 *
 *  A suspension isn't a failure of the OAuth round-trip — the provider vouched
 *  for them fine — so reporting it as one sends people to retry a login that
 *  will never work. Anything not listed here keeps the generic wording, which
 *  is right for the genuine OAuth faults (bad state, exchange failed). */
const OAUTH_ERRORS: Record<string, string> = {
  suspended: "This account has been suspended. Contact support if you think this is a mistake.",
  access_denied: "You cancelled the sign-in.",
  email_unverified: "Your account has no verified email address, so we can't sign you in this way.",
};

export default function AuthCallback() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { adoptToken } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const token = params.get("token");
    const loginError = params.get("login_error");
    if (loginError) { setError(OAUTH_ERRORS[loginError] || `Sign-in failed (${loginError}).`); return; }
    if (!token) { setError("No login token received."); return; }
    // Where to land. Connecting GitHub from the publish page comes back with
    // ?next=/publish so the user resumes what they were doing rather than being
    // dropped on the dashboard to navigate back themselves. Only a relative path
    // is followed (the server enforces the same thing) — an absolute URL here
    // would be an open redirect wearing a login page.
    const next = params.get("next") || "";
    const dest = next.startsWith("/") && !next.startsWith("//") ? next : "/dashboard";
    adoptToken(token)
      .then(() => navigate(dest, { replace: true }))
      .catch(() => setError("Could not complete login. Please try again."));
  }, []);

  return (
    <Page className="flex items-center justify-center min-h-[70vh]">
      <div className="glass rounded-3xl p-8 text-center max-w-sm">
        {error ? (
          <>
            <AlertTriangle size={28} className="text-rose-400 mx-auto mb-3" />
            <p className="text-white/80 text-sm mb-4">{error}</p>
            <button onClick={() => navigate("/login")} className="px-5 py-2.5 rounded-xl btn-primary text-sm font-semibold">Back to login</button>
          </>
        ) : (
          <>
            <Loader2 size={28} className="text-brand-300 mx-auto mb-3 animate-spin" />
            <p className="text-grey-300 text-sm">Signing you in…</p>
          </>
        )}
      </div>
    </Page>
  );
}
