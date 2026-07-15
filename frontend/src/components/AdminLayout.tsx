import { useState, useEffect } from "react";
import { NavLink, Outlet, Link, useLocation } from "react-router-dom";
import { Loader2, WifiOff, LogOut, ShieldAlert, Menu, X } from "lucide-react";
import { ADMIN_NAV_ITEMS } from "../nav";
import { useAuth } from "../context/AuthContext";
import AdminLogin from "../pages/AdminLogin";

/**
 * The admin portal: its own gate, its own shell, its own nav.
 *
 *  Standalone rather than a section of the signed-in app. /admin is reachable
 *  logged-out and answers with its own sign-in (AdminLogin) instead of bouncing
 *  to the product's /login, and once inside there is no Generate/Publish/Scan —
 *  only the console.
 *
 *  The gate is presentation. It decides which component renders; it grants
 *  nothing. Every /api/admin route re-derives the role from the server's
 *  ADMIN_EMAILS allowlist on every request and 404s otherwise, so editing
 *  `is_admin` in devtools produces an empty console whose fetches all fail.
 */
export default function AdminLayout() {
  const { user, loading, unavailable, retry, logout } = useAuth();
  const location = useLocation();
  const [open, setOpen] = useState(false);
  const [retrying, setRetrying] = useState(false);

  useEffect(() => { setOpen(false); }, [location.pathname]);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <Loader2 size={26} className="text-brand-300 animate-spin" />
      </div>
    );
  }

  // Couldn't reach the server to check the session. Showing the sign-in form
  // here would be a lie — it would reject a correct password, because there's
  // nothing to ask.
  if (unavailable) {
    const again = async () => {
      setRetrying(true);
      try { await retry(); } finally { setRetrying(false); }
    };
    return (
      <div className="flex flex-col items-center justify-center min-h-screen text-center px-6">
        <WifiOff size={26} className="text-amber-400 mb-3" />
        <p className="text-white/80 text-sm mb-1">Can’t reach the server.</p>
        <p className="text-grey-400 text-xs mb-4">The console will load once it’s back.</p>
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

  // Logged out, or logged in as someone without the role. AdminLogin covers
  // both — the second case gets a refusal panel rather than a form.
  if (!user?.is_admin) return <AdminLogin />;

  const links = (
    <nav className="flex flex-col gap-0.5">
      {ADMIN_NAV_ITEMS.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === "/admin"}
          className={({ isActive }) =>
            `flex items-center gap-3 px-3 py-2.5 rounded-xl text-[13px] font-medium transition-all ${
              isActive
                ? "text-white bg-white/10 shadow-inner-hi"
                : "text-white/[72%] hover:text-white hover:bg-white/5"
            }`
          }
        >
          {({ isActive }) => (
            <>
              <span
                aria-hidden
                className={`h-5 w-[3px] shrink-0 rounded-full transition-colors ${
                  isActive ? "bg-amber-400" : "bg-transparent"
                }`}
              />
              <Icon size={15} className="shrink-0" />
              {label}
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );

  return (
    <div className="min-h-screen">
      {/* Its own header, not the product NavBar. The amber rule is the standing
          reminder that actions here land on other people's accounts. */}
      <header className="sticky top-0 z-30 border-b border-amber-500/25 bg-ink-950/85 backdrop-blur-xl">
        <div className="mx-auto w-full max-w-6xl px-4 sm:px-6 h-14 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5 min-w-0">
            <button
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              aria-controls="admin-nav"
              className="lg:hidden text-grey-300 hover:text-white transition-colors"
            >
              {open ? <X size={18} /> : <Menu size={18} />}
            </button>
            <ShieldAlert size={15} className="text-amber-400 shrink-0" />
            <span className="font-display text-[15px] font-bold tracking-tight truncate">
              Testra <span className="text-amber-400">Admin</span>
            </span>
          </div>

          <div className="flex items-center gap-3 min-w-0">
            <span className="hidden sm:block text-[11px] text-grey-400 truncate max-w-[200px]">
              {user.email || user.name}
            </span>
            {/* A way back to the product. Not a nav item — the console is only
                the console — but being unable to leave without editing the URL
                is worse than one quiet link. */}
            <Link
              to="/dashboard"
              className="hidden sm:block text-[11px] text-grey-500 hover:text-grey-300 transition-colors"
            >
              Main app
            </Link>
            <button
              onClick={() => logout()}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg btn-ghost text-[11px] text-grey-300"
            >
              <LogOut size={12} /> Sign out
            </button>
          </div>
        </div>
      </header>

      <div className="mx-auto w-full max-w-6xl px-4 sm:px-6 py-6">
        {open && (
          <div id="admin-nav" className="lg:hidden mb-4 glass-strong rounded-2xl p-2 shadow-card">
            {links}
          </div>
        )}
        <div className="flex gap-6">
          <aside className="hidden lg:block w-52 shrink-0">
            <div className="sticky top-20 glass-strong rounded-2xl p-3 shadow-card">
              {links}
            </div>
          </aside>
          <div className="min-w-0 flex-1">
            <Outlet />
          </div>
        </div>
      </div>
    </div>
  );
}
