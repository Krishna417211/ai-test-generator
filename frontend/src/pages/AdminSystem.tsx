import { useEffect, useState, useCallback } from "react";
import {
  Loader2, RefreshCw, CheckCircle2, AlertTriangle, XCircle, Server, Clock,
  Database, KeyRound,
} from "lucide-react";
import Page from "../components/Page";
import { fetchAdminSystem, type AdminSystem as SystemData, type AdminKeyHealth } from "../utils/api";
import { pluralize, formatWhen, formatDuration } from "../utils/format";

/** How often the page re-reads. Key cooldowns are measured in seconds, so a
 *  static page would show a key as cooling long after it recovered. */
const POLL_MS = 15_000;

/** A key's state, as one of three things an operator can act on.
 *
 *  Deliberately three and not two: a 429 cooldown clears itself in a minute and
 *  needs nobody, while a hard block (out of credit, daily cap) never clears on
 *  its own and needs a human to add credit or a key. Collapsing them into one
 *  "unhealthy" would hide the only one that's actually an outage.
 */
function keyState(k: AdminKeyHealth): "ok" | "cooling" | "blocked" {
  if (k.hard_blocked) return "blocked";
  if (!k.available || k.cooldown_seconds_left > 0) return "cooling";
  return "ok";
}

const KEY_STATE = {
  ok: {
    icon: CheckCircle2, tint: "text-emerald-400",
    chip: "text-emerald-300 border-emerald-500/40 bg-emerald-500/10", label: "Live",
  },
  cooling: {
    icon: Clock, tint: "text-amber-400",
    chip: "text-amber-300 border-amber-500/40 bg-amber-500/10", label: "Cooling down",
  },
  blocked: {
    icon: XCircle, tint: "text-rose-400",
    chip: "text-rose-300 border-rose-500/40 bg-rose-500/10", label: "Out of quota",
  },
} as const;

function Tile({ icon: Icon, value, label }: { icon: any; value: string | number; label: string }) {
  // These tiles were built for short numbers. A word like "development" at
  // text-2xl runs straight out of the tile and into its neighbour, so anything
  // long steps down a size and truncates rather than overflowing.
  const long = String(value).length > 8;
  return (
    <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-5">
      <div className="w-9 h-9 rounded-xl bg-white/[0.06] border border-grey-700 flex items-center justify-center mb-3">
        <Icon size={16} className="text-grey-300" />
      </div>
      <div
        title={String(value)}
        className={`font-display font-bold text-white tabular-nums truncate ${long ? "text-base" : "text-2xl"}`}
      >
        {value}
      </div>
      <div className="text-xs text-grey-400 mt-0.5">{label}</div>
    </div>
  );
}

export default function AdminSystem() {
  const [data, setData] = useState<SystemData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await fetchAdminSystem());
      setError(null);
    } catch (e: any) {
      setError(e.message || "Could not load system status.");
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  if (error && !data) {
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
          <Loader2 size={16} className="animate-spin" /> Loading system status…
        </div>
      </Page>
    );
  }

  const liveKeys = data.keys.filter((k) => keyState(k) === "ok").length;
  const blocked = data.keys.filter((k) => keyState(k) === "blocked").length;

  return (
    <Page className="pb-8">
      <div className="space-y-6">
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div>
            <h1 className="font-display text-2xl font-bold tracking-tight">System</h1>
            <p className="text-sm text-grey-400 mt-1">
              Provider capacity and the switches this instance is running with.
            </p>
          </div>
          <button onClick={load} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg btn-ghost text-xs text-grey-300">
            <RefreshCw size={12} /> Refresh
          </button>
        </div>

        {/* Capacity gone is the one condition worth interrupting for: no key can
            serve a generation, for anyone, on any plan. */}
        {liveKeys === 0 && (
          <div className="flex items-start gap-2.5 px-4 py-3 rounded-xl bg-rose-500/15 border border-rose-500/30">
            <AlertTriangle size={15} className="text-rose-400 shrink-0 mt-0.5" />
            <div className="text-xs text-rose-200">
              <p className="font-medium">No LLM keys are available.</p>
              <p className="text-rose-300/80 mt-0.5">
                Generation is failing for every user right now — including Pro.
                {blocked > 0 && ` ${pluralize(blocked, "key")} out of quota; add credit or rotate below.`}
              </p>
            </div>
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Tile icon={Server} value={data.app_env} label="Environment" />
          <Tile icon={Clock} value={formatDuration(data.uptime_seconds)} label="Uptime" />
          <Tile icon={KeyRound} value={`${liveKeys}/${data.keys.length}`} label="Keys live" />
          <Tile icon={Database} value={data.jobs_stored} label="Jobs stored" />
        </div>

        <div className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-wider text-grey-400">
            Providers
          </div>
          <div className="grid sm:grid-cols-3 gap-3">
            {data.providers.map((p) => (
              <div key={p.name} className="rounded-2xl border border-grey-700 bg-white/[0.03] p-4">
                <div className="flex items-center justify-between gap-2 mb-2">
                  <span className="text-[13px] font-medium text-white capitalize">{p.name}</span>
                  {/* Icon + word, never colour alone. */}
                  <span className={`flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border font-medium ${
                    p.healthy ? KEY_STATE.ok.chip : KEY_STATE.blocked.chip}`}>
                    {p.healthy ? <CheckCircle2 size={9} /> : <XCircle size={9} />}
                    {p.healthy ? "Healthy" : "Down"}
                  </span>
                </div>
                <div className="text-[11px] text-grey-500">
                  {p.available_keys}/{p.total_keys} keys · {pluralize(p.total_calls, "call")}
                </div>
              </div>
            ))}
            {data.providers.length === 0 && (
              <div className="sm:col-span-3 rounded-xl border border-dashed border-grey-700 px-4 py-8 text-center text-xs text-grey-500">
                No providers configured — set at least one API key in the environment.
              </div>
            )}
          </div>
        </div>

        <div className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-wider text-grey-400">
            API keys
          </div>
          <div className="rounded-2xl border border-grey-700 bg-white/[0.03] overflow-hidden">
            {data.keys.length === 0 ? (
              <div className="px-4 py-10 text-center text-xs text-grey-500">No keys loaded.</div>
            ) : (
              data.keys.map((k) => {
                const state = keyState(k);
                const { icon: Icon, tint, chip, label } = KEY_STATE[state];
                return (
                  <div key={k.env_var}
                       className="border-b border-grey-800 last:border-b-0 px-4 py-3 flex items-center justify-between gap-3">
                    <div className="flex items-center gap-3 min-w-0">
                      <Icon size={14} className={`${tint} shrink-0`} />
                      <div className="min-w-0">
                        {/* The env var, never the key. */}
                        <div className="text-xs font-mono text-grey-200 truncate">{k.env_var}</div>
                        <div className="text-[10px] text-grey-500 mt-0.5">
                          {pluralize(k.call_count, "call")}
                          {k.error_count > 0 && ` · ${pluralize(k.error_count, "error")}`}
                          {k.last_used && ` · last used ${formatWhen(k.last_used)}`}
                        </div>
                      </div>
                    </div>
                    <span className={`flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border font-medium shrink-0 ${chip}`}>
                      {label}
                      {state === "cooling" && k.cooldown_seconds_left > 0 && ` · ${k.cooldown_seconds_left}s`}
                    </span>
                  </div>
                );
              })
            )}
          </div>
          {blocked > 0 && (
            <p className="text-[11px] text-grey-500">
              “Out of quota” means credit or a daily cap is exhausted — waiting won't
              clear it. Add credit or swap the key in the environment and restart.
            </p>
          )}
        </div>

        <div className="grid md:grid-cols-2 gap-6">
          <div className="space-y-3">
            <div className="text-xs font-semibold uppercase tracking-wider text-grey-400">
              Configuration
            </div>
            <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-4 space-y-2">
              {Object.entries(data.config).map(([key, value]) => (
                <div key={key} className="flex items-center justify-between gap-3">
                  <span className="text-[11px] text-grey-400 font-mono truncate">{key}</span>
                  {typeof value === "boolean" ? (
                    <span className={`flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border font-medium shrink-0 ${
                      value ? KEY_STATE.ok.chip : "text-grey-400 border-grey-600 bg-white/[0.06]"}`}>
                      {value ? <CheckCircle2 size={9} /> : <XCircle size={9} />}
                      {value ? "On" : "Off"}
                    </span>
                  ) : (
                    <span className="text-[11px] text-grey-300 tabular-nums shrink-0">{value}</span>
                  )}
                </div>
              ))}
            </div>
          </div>

          <div className="space-y-3">
            <div className="text-xs font-semibold uppercase tracking-wider text-grey-400">
              Recent LLM calls
            </div>
            <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-2 max-h-[280px] overflow-y-auto">
              {data.call_log.length === 0 ? (
                <div className="px-2 py-8 text-center text-xs text-grey-500">No calls yet.</div>
              ) : (
                [...data.call_log].reverse().map((c: any, i: number) => (
                  <div key={i} className="px-2 py-1.5 flex items-center justify-between gap-2 border-b border-grey-800 last:border-b-0">
                    <span className="flex items-center gap-1.5 min-w-0">
                      {c.success
                        ? <CheckCircle2 size={10} className="text-emerald-400 shrink-0" />
                        : <XCircle size={10} className="text-rose-400 shrink-0" />}
                      <span className="text-[11px] text-grey-300 truncate">
                        {c.provider}{c.context ? ` · ${c.context}` : ""}
                      </span>
                    </span>
                    <span className="text-[10px] text-grey-600 shrink-0 tabular-nums">
                      {c.latency_ms ? `${c.latency_ms}ms` : ""}
                    </span>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>
    </Page>
  );
}
