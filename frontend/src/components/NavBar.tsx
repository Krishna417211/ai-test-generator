import { useState, useRef, useEffect } from "react";
import { NavLink, Link, useNavigate, useLocation } from "react-router-dom";
import { TestTube2, Sparkles, Rocket, ShieldCheck, Activity, LogOut, UserRound, ChevronDown, Menu, X } from "lucide-react";
import { useAuth } from "../context/AuthContext";

const LINKS = [
  { to: "/generate", label: "Generate", icon: Sparkles },
  { to: "/publish", label: "Publish", icon: Rocket },
  { to: "/scan", label: "Scan", icon: ShieldCheck },
  { to: "/status", label: "Status", icon: Activity },
];

export default function NavBar() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [menu, setMenu] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setMenu(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  // Close the mobile nav whenever the route changes.
  useEffect(() => { setMobileNav(false); }, [location.pathname]);

  const initial = (user?.name || user?.email || "?").trim().charAt(0).toUpperCase();

  const doLogout = async () => { setMenu(false); await logout(); navigate("/"); };

  return (
    <header className="sticky top-0 z-40">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 pt-4">
        <nav className="glass-strong rounded-2xl px-3 sm:px-4 h-14 flex items-center justify-between shadow-card">
          <Link to="/" className="flex items-center gap-2.5 pl-1 group">
            <span className="relative w-8 h-8 rounded-xl bg-brand-gradient flex items-center justify-center shadow-glow">
              <TestTube2 size={16} className="text-ink-950" />
            </span>
            <span className="font-display font-bold text-[15px] tracking-[-0.01em]">Test<span className="text-gradient">ra</span></span>
          </Link>

          {user && (
            <div className="hidden md:flex items-center gap-1">
              {LINKS.map(({ to, label, icon: Icon }) => (
                <NavLink key={to} to={to}
                  className={({ isActive }) =>
                    `relative flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-[13px] font-medium transition-all ${
                      isActive ? "text-white bg-white/10 shadow-inner-hi" : "text-white/[72%] hover:text-white hover:bg-white/5"
                    }`}
                >
                  <Icon size={14} /> {label}
                </NavLink>
              ))}
            </div>
          )}

          <div className="flex items-center gap-2">
            {user && (
              <button
                onClick={() => setMobileNav((v) => !v)}
                aria-label="Toggle navigation menu"
                aria-expanded={mobileNav}
                className="md:hidden flex items-center justify-center w-9 h-9 rounded-xl btn-ghost text-white/80"
              >
                {mobileNav ? <X size={18} /> : <Menu size={18} />}
              </button>
            )}
            {user ? (
              <div className="relative" ref={ref}>
                <button onClick={() => setMenu(!menu)}
                  className="flex items-center gap-2 pl-1 pr-2 py-1 rounded-xl btn-ghost">
                  <span className="w-7 h-7 rounded-lg bg-brand-gradient flex items-center justify-center text-xs font-bold text-ink-950">{initial}</span>
                  <span className="hidden sm:block text-[13px] text-white/[72%] max-w-[120px] truncate">{user.name || user.email}</span>
                  <ChevronDown size={14} className="text-grey-400" />
                </button>
                {menu && (
                  <div className="absolute right-0 mt-2 w-56 glass-strong rounded-2xl p-2 shadow-card">
                    <div className="px-3 py-2 border-b border-grey-700 mb-1">
                      <div className="text-sm font-medium text-white truncate">{user.name || "Account"}</div>
                      <div className="text-xs text-grey-400 truncate">{user.email || (user.github_login ? `@${user.github_login}` : "")}</div>
                    </div>
                    <NavLink
                      to="/profile"
                      onClick={() => setMenu(false)}
                      className="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-sm text-white/80 hover:bg-white/8 transition-colors"
                    >
                      <UserRound size={15} /> Profile &amp; usage
                    </NavLink>
                    <button onClick={doLogout} className="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-sm text-white/80 hover:bg-white/8 transition-colors">
                      <LogOut size={15} /> Log out
                    </button>
                  </div>
                )}
              </div>
            ) : (
              <>
                <NavLink to="/login"
                  className={({ isActive }) =>
                    `hidden sm:inline-flex items-center px-3.5 py-2 rounded-xl text-[13px] font-medium transition-all ${
                      isActive ? "text-white bg-white/10" : "text-white/[72%] hover:text-white btn-ghost"
                    }`}
                >
                  Log in
                </NavLink>
                <NavLink to="/signup"
                  className={({ isActive }) =>
                    `inline-flex items-center gap-1.5 px-4 py-2 rounded-xl text-[13px] font-semibold transition-all ${
                      isActive ? "text-white bg-white/10" : "btn-primary"
                    }`}
                >
                  Sign up
                </NavLink>
              </>
            )}
          </div>
        </nav>

        {user && mobileNav && (
          <div className="md:hidden mt-2 glass-strong rounded-2xl p-2 shadow-card">
            {LINKS.map(({ to, label, icon: Icon }) => (
              <NavLink key={to} to={to}
                onClick={() => setMobileNav(false)}
                className={({ isActive }) =>
                  `flex items-center gap-2.5 px-3.5 py-3 rounded-xl text-sm font-medium transition-all ${
                    isActive ? "text-white bg-white/10" : "text-white/[72%] hover:text-white hover:bg-white/5"
                  }`}
              >
                <Icon size={16} /> {label}
              </NavLink>
            ))}
          </div>
        )}
      </div>
    </header>
  );
}
