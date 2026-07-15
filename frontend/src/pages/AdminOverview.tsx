import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import {
  Users, Sparkles, ShieldCheck, Github, Loader2, RefreshCw, AlertCircle,
  UserPlus, Ban, CreditCard, ArrowUpRight,
} from "lucide-react";
import Page from "../components/Page";
import ActivityChart from "../components/ActivityChart";
import { fetchAdminOverview, type AdminOverview as OverviewData } from "../utils/api";
import { pluralize, frameworkLabel, languageLabel, formatWhen, shortSource } from "../utils/format";
import { SERIES_COLOR } from "../utils/chartPalette";

function StatTile({ icon: Icon, value, label, sub, tone }: {
  icon: any; value: number | string; label: string; sub?: string;
  tone?: "warn";
}) {
  return (
    <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-5">
      <div className="w-9 h-9 rounded-xl bg-white/[0.06] border border-grey-700 flex items-center justify-center mb-3">
        <Icon size={16} className={tone === "warn" ? "text-amber-400" : "text-grey-300"} />
      </div>
      <div className="font-display text-2xl font-bold text-white tabular-nums">{value}</div>
      <div className="text-xs text-grey-400 mt-0.5">{label}</div>
      {sub && <div className="text-[11px] text-grey-500 mt-1">{sub}</div>}
    </div>
  );
}

/** A ranked breakdown: one bar per category, longest first.
 *
 *  Single hue, no legend — the bars encode magnitude only, and each one carries
 *  its own name, so colour has no identity to convey here. Giving each framework
 *  its own colour would invent a categorical scale that means nothing and would
 *  repaint itself every time the ranking shifted.
 */
function RankedBars({ title, rows, empty, label }: {
  title: string;
  rows: { name: string; count: number }[];
  empty: string;
  /** Turns the stored key into a name — the API sends generator identifiers
   *  ("playwright_js", "typescript"), never display text. */
  label: (key: string) => string;
}) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  const total = rows.reduce((a, r) => a + r.count, 0);

  return (
    <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-5">
      <div className="text-xs font-semibold uppercase tracking-wider text-grey-400">
        {title}
      </div>
      {rows.length === 0 ? (
        <div className="mt-4 rounded-xl border border-dashed border-grey-700 px-4 py-8 text-center text-xs text-grey-500">
          {empty}
        </div>
      ) : (
        <div className="mt-4 space-y-2.5">
          {rows.map((r) => (
            <div key={r.name}>
              <div className="flex items-baseline justify-between gap-3 mb-1">
                <span className="text-xs text-grey-300 truncate">{label(r.name)}</span>
                {/* The value is a direct label, so the bar never needs a tooltip
                    to be readable, and the text stays in an ink token. */}
                <span className="text-[11px] text-grey-500 tabular-nums shrink-0">
                  {r.count} · {Math.round((r.count / total) * 100)}%
                </span>
              </div>
              <div className="h-2 rounded-full bg-white/[0.06] overflow-hidden">
                <div
                  className="h-full rounded-full transition-all duration-700"
                  style={{ width: `${(r.count / max) * 100}%`, background: SERIES_COLOR.generate }}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function AdminOverview() {
  const [data, setData] = useState<OverviewData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await fetchAdminOverview());
      setError(null);
    } catch (e: any) {
      setError(e.message || "Could not load the overview.");
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (error) {
    return (
      <Page>
        <div className="max-w-lg mx-auto px-4 py-4 rounded-2xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300 text-center">
          {error}
          <button onClick={load} className="mt-3 mx-auto flex items-center gap-1.5 px-3 py-1.5 rounded-lg btn-ghost text-xs">
            <RefreshCw size={12} /> Try again
          </button>
        </div>
      </Page>
    );
  }

  if (!data) {
    return (
      <Page>
        <div className="flex items-center justify-center py-32 text-grey-400 gap-2 text-sm">
          <Loader2 size={16} className="animate-spin" /> Loading the overview…
        </div>
      </Page>
    );
  }

  const { totals, series, frameworks, languages, recent_failures } = data;
  const failRate = totals.generations > 0
    ? Math.round((totals.generations_failed / totals.generations) * 100)
    : 0;

  return (
    <Page className="pb-8">
      <div className="space-y-6">
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div>
            <h1 className="font-display text-2xl font-bold tracking-tight">Platform overview</h1>
            <p className="text-sm text-grey-400 mt-1">
              Every account on this instance, not just yours.
            </p>
          </div>
          <button onClick={load} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg btn-ghost text-xs text-grey-300">
            <RefreshCw size={12} /> Refresh
          </button>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatTile
            icon={Users} value={totals.users} label="Accounts"
            sub={totals.users_new_7d > 0 ? `+${totals.users_new_7d} this week` : "No new signups this week"}
          />
          <StatTile
            icon={CreditCard} value={totals.users_pro} label="On Pro"
            sub={totals.users > 0 ? `${Math.round((totals.users_pro / totals.users) * 100)}% of accounts` : undefined}
          />
          <StatTile
            icon={Sparkles} value={totals.generations} label="Suites generated"
            sub={`${totals.generations_24h} in the last 24h`}
          />
          <StatTile
            icon={ShieldCheck} value={totals.scans} label="Sites scanned"
          />
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatTile icon={Github} value={totals.published_repos} label="Repos published" />
          <StatTile icon={UserPlus} value={totals.active_sessions} label="Live sessions" />
          <StatTile
            icon={Ban} value={totals.users_suspended} label="Suspended"
            tone={totals.users_suspended > 0 ? "warn" : undefined}
          />
          <StatTile
            icon={AlertCircle} value={`${failRate}%`} label="Generation failure rate"
            tone={failRate >= 10 ? "warn" : undefined}
            sub={`${pluralize(totals.generations_failed, "failure")} all time`}
          />
        </div>

        {/* The same component the per-user dashboard uses. Its series slots are
            fixed by feature, so admin and user charts read identically — the
            green column means "generate" on both. */}
        <ActivityChart series={series} />

        <div className="grid md:grid-cols-2 gap-6">
          <RankedBars title="By framework" rows={frameworks} label={frameworkLabel}
                      empty="No suites generated yet." />
          <RankedBars title="By language" rows={languages} label={languageLabel}
                      empty="No suites generated yet." />
        </div>

        <div className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-wider text-grey-400">
            Recent failures
          </div>
          {recent_failures.length === 0 ? (
            <div className="rounded-xl border border-dashed border-grey-700 px-4 py-8 text-center text-xs text-grey-500">
              Nothing has failed recently.
            </div>
          ) : (
            <div className="space-y-2">
              {recent_failures.map((f: any) => (
                <div key={f.id}
                     className="rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-3 flex items-start justify-between gap-3">
                  <div className="flex items-start gap-3 min-w-0">
                    {/* Icon + text, never colour alone. */}
                    <AlertCircle size={13} className="text-rose-400 shrink-0 mt-0.5" />
                    <div className="min-w-0">
                      <div className="text-xs font-mono text-grey-200 truncate">
                        {shortSource(f.source || "")}
                      </div>
                      <div className="text-xs text-grey-500 mt-0.5 truncate">
                        {f.error || "Unknown error"}
                      </div>
                      {f.user_id && (
                        <Link to={`/admin/users?q=${encodeURIComponent(f.email || "")}`}
                              className="mt-1 inline-flex items-center gap-1 text-[11px] text-brand-300 hover:text-brand-200 transition-colors">
                          {f.email || f.user_id} <ArrowUpRight size={10} />
                        </Link>
                      )}
                    </div>
                  </div>
                  <span className="text-xs text-grey-500 shrink-0">{formatWhen(f.created_at)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </Page>
  );
}
