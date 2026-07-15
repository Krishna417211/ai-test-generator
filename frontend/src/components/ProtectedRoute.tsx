import { Navigate, useLocation } from "react-router-dom";
import { Loader2, WifiOff } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useAuth } from "../context/AuthContext";

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const { user, loading, unavailable, retry } = useAuth();
  const location = useLocation();
  const [retrying, setRetrying] = useState(false);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <Loader2 size={26} className="text-brand-300 animate-spin" />
      </div>
    );
  }
  // Couldn't reach the server to check the token. Sending the user to /login
  // would throw away the page they're on for what is probably a blip.
  if (unavailable) {
    const again = async () => {
      setRetrying(true);
      try { await retry(); } finally { setRetrying(false); }
    };
    return (
      <div className="flex flex-col items-center justify-center min-h-[60vh] text-center px-6">
        <WifiOff size={26} className="text-amber-400 mb-3" />
        <p className="text-white/80 text-sm mb-1">Can’t reach the server.</p>
        <p className="text-grey-400 text-xs mb-4">You’re still signed in — this page will load once it’s back.</p>
        <button
          onClick={again}
          disabled={retrying}
          className="px-5 py-2.5 rounded-xl btn-primary text-sm font-semibold disabled:opacity-60"
        >
          {retrying ? "Retrying…" : "Retry"}
        </button>
      </div>
    );
  }
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}
