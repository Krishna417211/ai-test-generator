import {
  LayoutDashboard, Sparkles, Rocket, ShieldCheck, Activity, Settings, UserRound,
  Gauge, Users, ServerCog,
} from "lucide-react";

/** The authenticated navigation, defined once.
 *
 *  The sidebar renders these on every signed-in page, so NavBar no longer
 *  carries its own copy of the links — two lists that had to be kept in step
 *  was exactly how /profile ended up reachable from one menu and not the other.
 */
export const NAV_ITEMS = [
  { to: "/dashboard", label: "Overview", icon: LayoutDashboard },
  { to: "/generate", label: "Generate", icon: Sparkles },
  { to: "/publish", label: "Publish", icon: Rocket },
  { to: "/scan", label: "Scan", icon: ShieldCheck },
  { to: "/status", label: "Status", icon: Activity },
  { to: "/settings", label: "Settings", icon: Settings },
  { to: "/profile", label: "Profile", icon: UserRound },
] as const;

/** The admin portal's nav. Rendered only by AdminLayout — never by the app's
 *  sidebar, which is why this is a separate list and not an `adminOnly` flag on
 *  NAV_ITEMS: /admin is a standalone console behind its own sign-in, not a
 *  section of the product, and a flag would invite something to render these
 *  next to "Generate" the moment someone forgot to filter on it.
 *
 *  Showing these is a UI decision only. Every route behind them is enforced by
 *  the server's ADMIN_EMAILS allowlist (backend/services/admin.py).
 */
export const ADMIN_NAV_ITEMS = [
  { to: "/admin", label: "Overview", icon: Gauge },
  { to: "/admin/users", label: "Users", icon: Users },
  { to: "/admin/system", label: "System", icon: ServerCog },
] as const;

/** Routes that render inside the *product* dashboard shell. The admin portal
 *  brings its own shell, so its routes are deliberately not here. */
export const SHELL_ROUTES: readonly string[] = NAV_ITEMS.map((i) => i.to);
