import { useState, useRef, useEffect } from "react";
import { NavLink, Link, useNavigate } from "react-router-dom";
import { TestTube2, LogOut, UserRound, LayoutDashboard, Settings, ChevronDown } from "lucide-react";
import { useAuth } from "../context/AuthContext";

export default function NavBar() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [menu, setMenu] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setMenu(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const initial = (user?.name || user?.email || "?").trim().charAt(0).toUpperCase();

  const doLogout = async () => { setMenu(false); await logout(); navigate("/"); };

  return (
    <header className="sticky top-0 z-40">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 pt-4">
        <nav className="glass-strong rounded-2xl px-3 sm:px-4 h-14 flex items-center justify-between shadow-card">
          {/* Signed in, the logo goes to the dashboard — that's the app's home
              once you have one; the marketing page isn't. */}
          <Link to={user ? "/dashboard" : "/"} className="flex items-center gap-2.5 pl-1 group">
            <span className="relative w-8 h-8 rounded-xl bg-brand-gradient flex items-center justify-center shadow-glow">
              <TestTube2 size={16} className="text-ink-950" />
            </span>
            <span className="font-display font-bold text-[15px] tracking-[-0.01em]">Test<span className="text-gradient">ra</span></span>
          </Link>

          {/* The feature links live in the dashboard sidebar, which is on every
              signed-in page — duplicating them here just gave us two lists to
              keep in step. */}

          <div className="flex items-center gap-2">
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
                    {[
                      { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
                      { to: "/profile", label: "Profile & usage", icon: UserRound },
                      { to: "/settings", label: "Settings", icon: Settings },
                    ].map(({ to, label, icon: Icon }) => (
                      <NavLink
                        key={to}
                        to={to}
                        onClick={() => setMenu(false)}
                        className="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-sm text-white/80 hover:bg-white/8 transition-colors"
                      >
                        <Icon size={15} /> {label}
                      </NavLink>
                    ))}
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
      </div>
    </header>
  );
}
