import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { getBillingInfo, type QuotaInfo } from "../utils/api";
import { formatDate, pluralize } from "../utils/format";

/** The signed-in usage meter in the nav.
 *
 *  Deliberately renders nothing in three cases, because a meter that can't say
 *  anything true is worse than no meter:
 *    - signed out       → there is no quota to speak of
 *    - Pro (limit null) → unmetered, so a bar has no denominator to fill
 *    - not loaded yet   → an empty bar reads as "zero left", the opposite of
 *                         the truth while the request is still in flight
 */
export default function QuotaBar() {
  const { user } = useAuth();
  const { pathname } = useLocation();
  const [quota, setQuota] = useState<QuotaInfo | null>(null);

  // Refetched on navigation as well as on login: generating spends a credit, so
  // a meter fetched once at mount would sit there stale for the whole session,
  // still promising runs the user has already used.
  useEffect(() => {
    if (!user) {
      setQuota(null);
      return;
    }
    let alive = true;
    getBillingInfo()
      .then((b) => alive && setQuota(b.quota))
      // Silent: this is ambient furniture, not something worth an error state.
      // The generate flow surfaces a real 402 if quota actually runs out.
      .catch(() => {});
    return () => { alive = false; };
  }, [user, pathname]);

  if (!user || !quota || quota.limit === null) return null;

  // used can exceed limit if the cap were ever lowered mid-month; clamping keeps
  // the bar from overflowing its track.
  const used = Math.min(quota.used, quota.limit);
  const pct = quota.limit > 0 ? Math.min(100, (used / quota.limit) * 100) : 100;
  const spent = quota.remaining === 0;

  return (
    <Link
      to="/settings"
      title={spent
        ? `No generations left — resets ${formatDate(quota.resets_at)}`
        : `${pluralize(quota.remaining ?? 0, "generation")} left this month`}
      className="group hidden sm:flex items-center gap-2.5 px-3 py-1.5 rounded-xl hover:bg-white/[0.06] transition-colors"
    >
      <div className="flex flex-col gap-1.5 w-28">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[11px] font-medium text-white/[72%] leading-none">
            {/* Once it's full, the count stops being the useful number and the
                reset date starts — that's the only thing left to act on. */}
            {spent ? "Resets" : `${quota.remaining} left`}
          </span>
          <span className="text-[10px] text-grey-500 leading-none tabular-nums">
            {spent ? formatDate(quota.resets_at) : `${used}/${quota.limit}`}
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-white/[0.08] overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-700 ${
              spent ? "bg-amber-400" : "bg-brand-gradient"}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
    </Link>
  );
}
