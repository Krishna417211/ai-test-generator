import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import {
  Sparkles, Rocket, ShieldCheck, FileCode, Loader2, Gauge, ArrowUpRight,
  AlertCircle, Github, RefreshCw,
} from "lucide-react";
import Page from "../components/Page";
import ActivityChart from "../components/ActivityChart";
import { getDashboard } from "../utils/api";
import { pluralize, frameworkLabel, formatDate as fmtDate, formatWhen as fmtWhen, shortSource } from "../utils/format";
import { SERIES_COLOR } from "../utils/chartPalette";
import type { Dashboard as DashboardData, ActivityItem } from "../types";

const GRADE_TINT: Record<string, string> = {
  A: "text-emerald-400 border-emerald-500/40 bg-emerald-500/10",
  B: "text-brand-400 border-brand-500/40 bg-brand-500/10",
  C: "text-amber-400 border-amber-500/40 bg-amber-500/10",
  D: "text-amber-500 border-amber-500/50 bg-amber-500/15",
  F: "text-rose-500 border-rose-500/40 bg-rose-500/10",
};

function StatTile({ icon: Icon, value, label, sub }: {
  icon: any; value: number; label: string; sub?: string;
}) {
  return (
    <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-5">
      <div className="w-9 h-9 rounded-xl bg-white/[0.06] border border-grey-700 flex items-center justify-center mb-3">
        <Icon size={16} className="text-grey-300" />
      </div>
      <div className="font-display text-2xl font-bold text-white tabular-nums">{value}</div>
      <div className="text-xs text-grey-400 mt-0.5">{label}</div>
      {sub && <div className="text-[11px] text-grey-500 mt-1">{sub}</div>}
    </div>
  );
}

/** One row of the unified feed. The `kind` decides the shape, so each branch
 *  reads the fields that actually exist on it. */
function ActivityRow({ item }: { item: ActivityItem }) {
  const dot = (
    <span className="w-2 h-2 shrink-0 rounded-[3px]" style={{ background: SERIES_COLOR[item.kind] }} />
  );
  const when = <span className="text-xs text-grey-500 shrink-0">{fmtWhen(item.created_at)}</span>;

  if (item.kind === "generate") {
    const failed = item.status === "failed";
    return (
      <div className="rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          {dot}
          <div className="min-w-0">
            <div className="text-xs font-mono text-grey-200 truncate">{shortSource(item.source)}</div>
            <div className="text-xs text-grey-500 mt-0.5 flex items-center gap-1.5">
              {failed ? (
                // Status is never colour-alone: icon + word, both.
                <span className="flex items-center gap-1 text-rose-400">
                  <AlertCircle size={11} /> Generation failed
                </span>
              ) : (
                <>Generated {frameworkLabel(item.framework)} · {pluralize(item.test_count, "test")}</>
              )}
            </div>
          </div>
        </div>
        {when}
      </div>
    );
  }

  if (item.kind === "publish") {
    return (
      <a href={item.repo_url} target="_blank" rel="noopener noreferrer"
         className="rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-3 flex items-center justify-between gap-3 transition-colors hover:border-grey-600 group">
        <div className="flex items-center gap-3 min-w-0">
          {dot}
          <div className="min-w-0">
            <div className="text-xs font-mono text-grey-200 truncate group-hover:text-white transition-colors">
              {item.full_name}
            </div>
            <div className="text-xs text-grey-500 mt-0.5">
              Published {pluralize(item.files_pushed, "file")}
              {item.test_count > 0 && <> · {pluralize(item.test_count, "test")}</>}
            </div>
          </div>
        </div>
        <span className="flex items-center gap-1.5 shrink-0">
          {when}
          <ArrowUpRight size={13} className="text-grey-500 opacity-0 group-hover:opacity-100 transition-opacity" />
        </span>
      </a>
    );
  }

  return (
    <div className="rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-3 flex items-center justify-between gap-3">
      <div className="flex items-center gap-3 min-w-0">
        {dot}
        <span className={`w-8 h-8 shrink-0 rounded-lg border flex items-center justify-center font-display font-bold text-xs ${GRADE_TINT[item.grade] || GRADE_TINT.F}`}>
          {item.grade}
        </span>
        <div className="min-w-0">
          <div className="text-xs font-mono text-grey-200 truncate">{shortSource(item.url)}</div>
          <div className="text-xs text-grey-500 mt-0.5">
            Scanned · {item.score}/100 · {pluralize(item.findings, "finding")}
          </div>
        </div>
      </div>
      {when}
    </div>
  );
}

const QUICK_ACTIONS = [
  { to: "/generate", label: "Generate tests", icon: Sparkles,
    hint: "Point at a repo, get an E2E suite" },
  { to: "/publish", label: "Publish a project", icon: Rocket,
    hint: "Push to a new GitHub repo with CI" },
  { to: "/scan", label: "Scan a site", icon: ShieldCheck,
    hint: "Audit a deployed URL for issues" },
];

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await getDashboard());
      setError(null);
    } catch (e: any) {
      setError(e.message || "Could not load your dashboard.");
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
          <Loader2 size={16} className="animate-spin" /> Loading your dashboard…
        </div>
      </Page>
    );
  }

  const { user, plan, totals, activity, series } = data;
  const isPro = plan.plan !== "free";
  const usedPct = plan.limit ? Math.min(100, Math.round((plan.used / plan.limit) * 100)) : 0;
  const firstName = (user.name || "").split(" ")[0] || "there";

  return (
    <Page className="pb-8">
      <div className="space-y-6">
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div>
            <h1 className="font-display text-2xl font-bold tracking-tight">
              Welcome back, {firstName}
            </h1>
            <p className="text-sm text-grey-400 mt-1">
              {totals.generations + totals.scans + totals.repos_published === 0
                ? "Nothing here yet — pick a place to start below."
                : "Here's what you've been doing with Testra."}
            </p>
          </div>
          <span className={`text-[11px] px-2 py-0.5 rounded-full border font-medium ${
            isPro ? "text-brand-200 border-brand-400/40 bg-brand-400/10"
                  : "text-grey-300 border-grey-600 bg-white/[0.06]"}`}>
            {isPro ? "Pro" : "Free"}
          </span>
        </div>

        {/* Lifetime totals — headline numbers, so tiles rather than a chart. */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatTile
            icon={Sparkles} value={totals.generations} label="Suites generated"
            sub={totals.generations_failed > 0
              ? `${totals.generations_failed} failed`
              : undefined}
          />
          <StatTile icon={FileCode} value={totals.tests_written} label="Tests written" />
          <StatTile icon={Github} value={totals.repos_published} label="Repos published" />
          <StatTile icon={ShieldCheck} value={totals.scans} label="Sites scanned" />
        </div>

        <ActivityChart series={series} />

        <div className="grid lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-3">
            <div className="flex items-center justify-between gap-3">
              <div className="text-xs font-semibold uppercase tracking-wider text-grey-400">
                Recent activity
              </div>
              {/* The feed is capped, so say where the rest lives rather than
                  just ending. */}
              {activity.length > 0 && (
                <Link to="/profile"
                      className="flex items-center gap-1 text-[11px] text-brand-300 hover:text-brand-200 transition-colors">
                  Full history <ArrowUpRight size={11} />
                </Link>
              )}
            </div>
            {activity.length === 0 ? (
              <div className="rounded-xl border border-dashed border-grey-700 px-4 py-8 text-center text-xs text-grey-500">
                Your generates, publishes, and scans will appear here as you go.
              </div>
            ) : (
              <div className="space-y-2">
                {activity.map((item, i) => (
                  <ActivityRow key={`${item.kind}-${item.created_at}-${i}`} item={item} />
                ))}
              </div>
            )}
          </div>

          <div className="space-y-6">
            {/* Quota: a single ratio against a limit — a meter, not a chart. */}
            <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-5">
              <div className="flex items-center gap-2 mb-4">
                <Gauge size={14} className="text-grey-400" />
                <span className="text-xs font-semibold text-grey-400 uppercase tracking-wider">
                  Plan &amp; usage
                </span>
              </div>
              <div className="flex items-baseline justify-between mb-2 gap-2">
                <span className="text-sm text-grey-300">
                  {isPro ? "Unlimited" : `${plan.used} of ${plan.limit}`}
                </span>
                <span className="text-[11px] text-grey-500">
                  {isPro
                    ? plan.expires_at ? `Renews ${fmtDate(plan.expires_at)}` : "Active"
                    : `Resets ${fmtDate(plan.resets_at)}`}
                </span>
              </div>
              {!isPro && plan.limit !== null && (
                <>
                  <div className="h-2 rounded-full bg-white/[0.06] overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all duration-700 ${
                        usedPct >= 100 ? "bg-amber-400" : "bg-brand-gradient"}`}
                      style={{ width: `${usedPct}%` }}
                    />
                  </div>
                  <p className="mt-3 text-[11px] text-grey-500">
                    {plan.remaining === 0
                      ? "You're out of generations this month."
                      : `${pluralize(plan.remaining ?? 0, "generation")} left this month.`}
                  </p>
                  <Link to="/settings"
                        className="mt-3 inline-flex items-center gap-1 text-[11px] text-brand-300 hover:text-brand-200 transition-colors">
                    Manage plan <ArrowUpRight size={11} />
                  </Link>
                </>
              )}
            </div>

            <div className="space-y-2">
              <div className="text-xs font-semibold uppercase tracking-wider text-grey-400">
                Quick actions
              </div>
              {QUICK_ACTIONS.map(({ to, label, icon: Icon, hint }) => (
                <Link key={to} to={to}
                      className="flex items-center gap-3 rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-3 transition-colors hover:border-grey-600 group">
                  <span className="w-8 h-8 shrink-0 rounded-lg bg-white/[0.06] border border-grey-700 flex items-center justify-center">
                    <Icon size={14} className="text-grey-300" />
                  </span>
                  <span className="min-w-0">
                    <span className="block text-[13px] font-medium text-white truncate">{label}</span>
                    <span className="block text-[11px] text-grey-500 truncate">{hint}</span>
                  </span>
                  <ArrowUpRight size={13} className="ml-auto shrink-0 text-grey-500 opacity-0 group-hover:opacity-100 transition-opacity" />
                </Link>
              ))}
            </div>
          </div>
        </div>
      </div>
    </Page>
  );
}
