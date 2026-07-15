import { useEffect, useState, useCallback } from "react";
import {
  UserRound, Sparkles, ShieldCheck, Github, FileCode, Loader2,
  ArrowUpRight, Check, AlertCircle, Gauge,
} from "lucide-react";
import Page from "../components/Page";
import PageHeader from "../components/PageHeader";
import { getProfile, startCheckout, CheckoutUnavailableError } from "../utils/api";
import { pluralize, frameworkLabel } from "../utils/format";
import type { Profile as ProfileData, PlanCatalogueEntry } from "../types";

const PRO_PERKS = [
  "Unlimited test generations",
  "Priority access when capacity is tight",
  "Page Object Models + CI/CD pipelines",
  "Private repo publishing",
];

const GRADE_TINT: Record<string, string> = {
  A: "text-emerald-400 border-emerald-500/40 bg-emerald-500/10",
  B: "text-brand-400 border-brand-500/40 bg-brand-500/10",
  C: "text-amber-400 border-amber-500/40 bg-amber-500/10",
  D: "text-amber-500 border-amber-500/50 bg-amber-500/15",
  F: "text-rose-500 border-rose-500/40 bg-rose-500/10",
};

function fmtDate(epochSeconds: number | null): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
    year: "numeric", month: "short", day: "numeric",
  });
}

function fmtWhen(epochSeconds: number): string {
  const then = new Date(epochSeconds * 1000);
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${pluralize(mins, "min")} ago`;
  if (mins < 60 * 24) return `${pluralize(Math.round(mins / 60), "hour")} ago`;
  if (mins < 60 * 24 * 7) return `${pluralize(Math.round(mins / 1440), "day")} ago`;
  return fmtDate(epochSeconds);
}

/** A repo URL is long and mostly boilerplate — show the part that identifies it. */
function shortSource(source: string): string {
  if (!source) return "ZIP upload";
  const m = source.match(/github\.com\/([^/]+\/[^/?#]+)/i);
  return m ? m[1] : source.replace(/^https?:\/\//, "");
}

function StatTile({ icon: Icon, value, label }: { icon: any; value: number; label: string }) {
  return (
    <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-5">
      <div className="w-9 h-9 rounded-xl bg-white/[0.06] border border-grey-700 flex items-center justify-center mb-3">
        <Icon size={16} className="text-grey-300" />
      </div>
      <div className="font-display text-2xl font-bold text-white">{value}</div>
      <div className="text-xs text-grey-400 mt-0.5">{label}</div>
    </div>
  );
}

function Section({ title, count, empty, children }: {
  title: string; count: number; empty: string; children?: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-xs font-semibold uppercase tracking-wider mb-3 text-grey-400">
        {title} {count > 0 && `(${count})`}
      </div>
      {count === 0 ? (
        <div className="rounded-xl border border-dashed border-grey-700 px-4 py-6 text-center text-xs text-grey-500">
          {empty}
        </div>
      ) : (
        <div className="space-y-2">{children}</div>
      )}
    </div>
  );
}

function UpgradePanel({ plan }: { plan: ProfileData["plan"] }) {
  const [selected, setSelected] = useState<"monthly" | "yearly">("yearly");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const byId = (id: string) => plan.catalogue.find((p) => p.id === id);
  const yearly = byId("yearly");
  const monthly = byId("monthly");
  const chosen: PlanCatalogueEntry | undefined = byId(selected);

  const upgrade = async () => {
    setLoading(true);
    setError(null);
    try {
      window.location.href = await startCheckout(selected);
    } catch (err: any) {
      setError(
        err instanceof CheckoutUnavailableError
          ? "Checkout isn't live yet — we can't take payments through the app right now."
          : err.message || "Could not start checkout.",
      );
      setLoading(false);
    }
  };

  return (
    <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-6">
      <h2 className="font-display text-xl font-bold tracking-tight">
        Want a better experience? Go <span className="text-gradient">Pro</span>
      </h2>
      <p className="mt-1.5 text-sm text-grey-400 leading-relaxed">
        {plan.limit === null
          ? "You're on Pro — generations are unlimited."
          : `You get ${plan.limit} generations a month on Free. Pro removes the cap.`}
      </p>

      <div className="mt-5 grid sm:grid-cols-2 gap-2.5">
        {[yearly, monthly].filter(Boolean).map((p) => (
          <button
            key={p!.id}
            onClick={() => setSelected(p!.id)}
            aria-pressed={selected === p!.id}
            className={`flex items-center justify-between rounded-2xl border p-4 text-left transition ${
              selected === p!.id
                ? "border-grey-300 bg-white/[0.08] shadow-inner-hi"
                : "border-grey-700 bg-white/[0.02] hover:border-grey-600"
            }`}
          >
            <span className="flex items-center gap-3 min-w-0">
              <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
                selected === p!.id ? "border-brand-400 bg-brand-400" : "border-grey-600"
              }`}>
                {selected === p!.id && <Check size={10} className="text-ink-950" strokeWidth={3.5} />}
              </span>
              <span className="font-semibold capitalize text-sm truncate">{p!.id}</span>
              {!!p!.savings_usd && (
                <span className="rounded-full bg-brand-400/15 px-2 py-0.5 text-[11px] font-medium text-brand-200 shrink-0">
                  Save ${p!.savings_usd}
                </span>
              )}
            </span>
            <span className="font-display text-lg font-bold shrink-0">
              ${p!.price_usd}
              <span className="text-xs font-normal text-grey-400">
                /{p!.interval === "year" ? "yr" : "mo"}
              </span>
            </span>
          </button>
        ))}
      </div>

      <ul className="mt-5 grid sm:grid-cols-2 gap-y-2 gap-x-4">
        {PRO_PERKS.map((perk) => (
          <li key={perk} className="flex items-center gap-2.5 text-sm text-grey-300">
            <Check size={14} className="shrink-0 text-brand-400" strokeWidth={3} />
            {perk}
          </li>
        ))}
      </ul>

      {/* The server has no payment provider wired up yet, so say that plainly
          up front rather than letting the button 503 on click. */}
      {!plan.checkout_available && (
        <div className="mt-5 flex items-start gap-2.5 rounded-xl border border-amber-400/30 bg-amber-400/10 p-3 text-xs leading-relaxed text-amber-300">
          <AlertCircle size={14} className="mt-0.5 shrink-0" />
          <span>Checkout isn't live yet — payments aren't connected to this app right now.</span>
        </div>
      )}

      {error && (
        <div role="alert" className="mt-5 flex items-start gap-2.5 rounded-xl border border-amber-400/30 bg-amber-400/10 p-3 text-xs leading-relaxed text-amber-300">
          <AlertCircle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <button
        onClick={upgrade}
        disabled={loading || !plan.checkout_available}
        className="btn-primary mt-5 flex w-full items-center justify-center gap-2 rounded-xl py-3.5 font-semibold disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {loading ? (
          <><Loader2 size={16} className="animate-spin" /> Starting checkout…</>
        ) : (
          <>Upgrade to Pro — ${chosen?.price_usd}{selected === "yearly" ? "/yr" : "/mo"}</>
        )}
      </button>
    </div>
  );
}

export default function Profile() {
  const [data, setData] = useState<ProfileData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await getProfile());
      setError(null);
    } catch (e: any) {
      setError(e.message || "Could not load your profile.");
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (error) {
    return (
      <Page>
        <PageHeader icon={UserRound} eyebrow="Your account" title="Profile" subtitle="" />
        <div className="max-w-lg mx-auto px-4 py-4 rounded-2xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300 text-center">
          {error}
        </div>
      </Page>
    );
  }

  if (!data) {
    return (
      <Page>
        <div className="flex items-center justify-center py-32 text-grey-400 gap-2 text-sm">
          <Loader2 size={16} className="animate-spin" /> Loading your profile…
        </div>
      </Page>
    );
  }

  const { user, plan, totals } = data;
  const isPro = plan.plan !== "free";
  const initial = (user.name || user.email || "?").charAt(0).toUpperCase();
  const usedPct = plan.limit ? Math.min(100, Math.round((plan.used / plan.limit) * 100)) : 0;

  return (
    <Page>
      <div className="w-full max-w-3xl mx-auto space-y-8 pt-4">
        {/* Identity */}
        <div className="flex items-center gap-4">
          {user.avatar_url ? (
            <img src={user.avatar_url} alt="" className="w-16 h-16 rounded-2xl border border-grey-700" />
          ) : (
            <div className="w-16 h-16 rounded-2xl bg-brand-400/15 border border-brand-400/30 flex items-center justify-center font-display text-2xl font-bold text-brand-200">
              {initial}
            </div>
          )}
          <div className="min-w-0">
            <div className="flex items-center gap-2.5 flex-wrap">
              <h1 className="font-display text-2xl font-bold tracking-tight truncate">{user.name}</h1>
              <span className={`text-[11px] px-2 py-0.5 rounded-full border font-medium ${
                isPro
                  ? "text-brand-200 border-brand-400/40 bg-brand-400/10"
                  : "text-grey-300 border-grey-600 bg-white/[0.06]"
              }`}>
                {isPro ? "Pro" : "Free"}
              </span>
            </div>
            <p className="text-sm text-grey-400 mt-0.5 truncate">{user.email}</p>
            <p className="text-xs text-grey-500 mt-1">
              Member since {fmtDate(data.member_since)}
              {user.github_login && <> · connected as @{user.github_login}</>}
            </p>
          </div>
        </div>

        {/* Lifetime totals */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatTile icon={Sparkles} value={totals.generations} label="Suites generated" />
          <StatTile icon={FileCode} value={totals.tests_written} label="Tests written" />
          <StatTile icon={ShieldCheck} value={totals.scans} label="Sites scanned" />
          <StatTile icon={Github} value={totals.repos_published} label="Repos published" />
        </div>

        {/* Plan & usage */}
        <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-5">
          <div className="flex items-center gap-2 mb-4">
            <Gauge size={14} className="text-grey-400" />
            <span className="text-xs font-semibold text-grey-400 uppercase tracking-wider">
              Plan &amp; usage
            </span>
          </div>

          <div className="flex items-baseline justify-between mb-2">
            <span className="text-sm text-grey-300">
              {isPro ? "Pro — unlimited generations" : `${plan.used} of ${plan.limit} generations used`}
            </span>
            <span className="text-xs text-grey-500">
              {isPro
                ? plan.expires_at ? `Renews ${fmtDate(plan.expires_at)}` : "Active"
                : `Resets ${fmtDate(plan.resets_at)}`}
            </span>
          </div>

          {!isPro && plan.limit !== null && (
            <div className="h-2 rounded-full bg-white/[0.06] overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-700 ${
                  usedPct >= 100 ? "bg-amber-400" : "bg-brand-gradient"
                }`}
                style={{ width: `${usedPct}%` }}
              />
            </div>
          )}

          {/* Nobody has paid on this server yet — there is no invoice, amount, or
              receipt to render, so state the plan honestly instead of an empty
              billing panel. */}
          <p className="mt-3 text-[11px] text-grey-500">
            {isPro
              ? "Thanks for supporting Testra."
              : "You're on the free plan — no payment on file."}
          </p>
        </div>

        {/* Work history */}
        <Section
          title="Test suites"
          count={data.generations.length}
          empty="No suites yet. Generate one and it'll show up here."
        >
          {data.generations.map((g, i) => (
            <div key={i} className="rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-3 flex items-center justify-between gap-3 transition-colors hover:border-grey-600">
              <div className="min-w-0">
                <div className="text-xs font-mono text-grey-200 truncate">{shortSource(g.source)}</div>
                <div className="text-xs text-grey-500 mt-0.5">
                  {frameworkLabel(g.framework)} · {pluralize(g.test_count, "test")} · {pluralize(g.file_count, "file")}
                </div>
              </div>
              <span className="text-xs text-grey-500 shrink-0">{fmtWhen(g.created_at)}</span>
            </div>
          ))}
        </Section>

        <Section
          title="Security scans"
          count={data.scans.length}
          empty="No scans yet. Scan a deployed site and it'll show up here."
        >
          {data.scans.map((s, i) => (
            <div key={i} className="rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-3 flex items-center justify-between gap-3 transition-colors hover:border-grey-600">
              <div className="flex items-center gap-3 min-w-0">
                <span className={`w-9 h-9 shrink-0 rounded-lg border flex items-center justify-center font-display font-bold text-sm ${GRADE_TINT[s.grade] || GRADE_TINT.F}`}>
                  {s.grade}
                </span>
                <div className="min-w-0">
                  <div className="text-xs font-mono text-grey-200 truncate">{shortSource(s.url)}</div>
                  <div className="text-xs text-grey-500 mt-0.5">
                    {s.score}/100 · {pluralize(s.findings, "finding")}
                  </div>
                </div>
              </div>
              <span className="text-xs text-grey-500 shrink-0">{fmtWhen(s.created_at)}</span>
            </div>
          ))}
        </Section>

        <Section
          title="Published repos"
          count={data.repos.length}
          empty="Nothing published yet. Push a project and it'll show up here."
        >
          {data.repos.map((r) => (
            <a
              key={r.full_name}
              href={r.repo_url}
              target="_blank"
              rel="noopener noreferrer"
              className="rounded-xl border border-grey-700 bg-white/[0.02] px-4 py-3 flex items-center justify-between gap-3 transition-colors hover:border-grey-600 group"
            >
              <div className="flex items-center gap-3 min-w-0">
                <Github size={15} className="text-grey-500 shrink-0" />
                <span className="text-xs font-mono text-grey-200 truncate group-hover:text-white transition-colors">
                  {r.full_name}
                </span>
              </div>
              <span className="flex items-center gap-1.5 text-xs text-grey-500 shrink-0">
                {fmtWhen(r.created_at)}
                <ArrowUpRight size={13} className="opacity-0 group-hover:opacity-100 transition-opacity" />
              </span>
            </a>
          ))}
        </Section>

        {/* Upgrade CTA — last thing on the page, per the brief. */}
        {!isPro && <UpgradePanel plan={plan} />}
      </div>
    </Page>
  );
}
