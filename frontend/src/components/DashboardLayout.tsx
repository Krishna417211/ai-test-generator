import { useState, useEffect } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { Menu, X, type LucideIcon } from "lucide-react";
import { NAV_ITEMS } from "../nav";
import { useAuth } from "../context/AuthContext";

/** The signed-in shell: a persistent nav rail on the left, page content right.
 *
 *  Rendered as a layout route, so the sidebar is mounted once and survives
 *  navigation between the pages inside it — the rail doesn't re-animate on every
 *  route change, and only <Outlet/> swaps.
 */
export default function DashboardLayout() {
  const { user } = useAuth();
  const location = useLocation();
  const [open, setOpen] = useState(false);

  // Close the drawer on navigation — on mobile it covers the page it just
  // navigated to, so leaving it open would hide the result of the tap.
  useEffect(() => { setOpen(false); }, [location.pathname]);

  const initial = (user?.name || user?.email || "?").trim().charAt(0).toUpperCase();

  const navLink = ({ to, label, icon: Icon }: { to: string; label: string; icon: LucideIcon }) => (
    <NavLink
      key={to}
      to={to}
      // `end` on the section roots only: without it /admin would stay marked
      // active while you're on /admin/users, lighting up two rows at once.
      end={to === "/dashboard" || to === "/admin"}
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
          {/* The active rail marker is a shape, not just a tint — a colour
              wash alone is easy to miss at this size. */}
          <span
            aria-hidden
            className={`h-5 w-[3px] shrink-0 rounded-full transition-colors ${
              isActive ? "bg-brand-gradient" : "bg-transparent"
            }`}
          />
          <Icon size={15} className="shrink-0" />
          {label}
        </>
      )}
    </NavLink>
  );

  // No admin links here on purpose. The console is a separate portal with its
  // own sign-in at /admin (see AdminLayout) — surfacing it as a row next to
  // "Generate" would blur the line this split exists to draw.
  const links = <nav className="flex flex-col gap-0.5">{NAV_ITEMS.map(navLink)}</nav>;

  return (
    <div className="relative z-10 mx-auto w-full max-w-6xl px-4 sm:px-6">
      {/* Mobile: the rail collapses to a disclosure above the content. */}
      <div className="lg:hidden mb-4">
        <button
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls="dash-nav"
          className="flex items-center gap-2 px-3.5 py-2.5 rounded-xl glass-strong text-[13px] font-medium text-white/80 shadow-card"
        >
          {open ? <X size={16} /> : <Menu size={16} />} Menu
        </button>
        {open && (
          <div id="dash-nav" className="mt-2 glass-strong rounded-2xl p-2 shadow-card">
            {links}
          </div>
        )}
      </div>

      <div className="flex gap-6">
        <aside className="hidden lg:block w-56 shrink-0">
          <div className="sticky top-24 glass-strong rounded-2xl p-3 shadow-card">
            <div className="flex items-center gap-2.5 px-2 py-2 mb-2 border-b border-grey-700">
              <span className="w-8 h-8 shrink-0 rounded-lg bg-brand-gradient flex items-center justify-center text-xs font-bold text-ink-950">
                {initial}
              </span>
              <div className="min-w-0">
                <div className="text-[13px] font-medium text-white truncate">
                  {user?.name || "Account"}
                </div>
                <div className="text-[11px] text-grey-400 truncate">
                  {user?.email || (user?.github_login ? `@${user.github_login}` : "")}
                </div>
              </div>
            </div>
            {links}
          </div>
        </aside>

        <div className="min-w-0 flex-1">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
