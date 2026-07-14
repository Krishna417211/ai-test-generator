import { useEffect, useState } from "react";
import { Activity, Cpu, Clock, Database, Zap, RefreshCw } from "lucide-react";
import Page from "../components/Page";
import PageHeader from "../components/PageHeader";
import { getProviderStatus, getMetrics } from "../utils/api";

const PROVIDER_META: Record<string, { label: string; emoji: string }> = {
  gemini: { label: "Google Gemini", emoji: "✨" },
  groq: { label: "Groq LLaMA", emoji: "⚡" },
  claude: { label: "Anthropic Claude", emoji: "🧠" },
};

function fmtUptime(s: number) {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export default function Status() {
  const [providers, setProviders] = useState<any[]>([]);
  const [metrics, setMetrics] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticking, setTicking] = useState(false);

  const load = async () => {
    setTicking(true);
    try {
      const [s, m] = await Promise.all([getProviderStatus(), getMetrics().catch(() => null)]);
      setProviders(s.providers || []); setMetrics(m); setError(null);
    } catch (e: any) {
      setError("Backend unreachable — is the API running on :8000?");
    } finally { setTimeout(() => setTicking(false), 400); }
  };

  useEffect(() => { load(); const id = setInterval(load, 5000); return () => clearInterval(id); }, []);

  const allHealthy = providers.length > 0 && providers.every((p) => p.healthy);
  const stat = (icon: any, label: string, value: string, tint: string) => {
    const Icon = icon;
    return (
      <div className="glass rounded-2xl p-5">
        <div className={`w-10 h-10 rounded-xl flex items-center justify-center mb-3 ${tint}`}><Icon size={18} /></div>
        <div className="font-display text-2xl font-bold">{value}</div>
        <div className="text-xs text-white/60 mt-0.5">{label}</div>
      </div>
    );
  };

  return (
    <Page>
      <PageHeader icon={Activity} eyebrow="Live system status"
        title="Provider health & metrics"
        subtitle="Real-time view of the free LLM providers powering Testra, auto-refreshed every few seconds." />

      {error ? (
        <div className="max-w-lg mx-auto px-4 py-4 rounded-2xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300 text-center">{error}</div>
      ) : (
        <div className="max-w-4xl mx-auto space-y-6">
          {/* Overall banner */}
          <div className={`flex items-center justify-between rounded-2xl px-5 py-4 border ${allHealthy ? "bg-emerald-500/10 border-emerald-500/30" : "bg-amber-500/10 border-amber-500/30"}`}>
            <div className="flex items-center gap-3">
              <span className={`relative flex h-3 w-3`}>
                <span className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-60 ${allHealthy ? "bg-emerald-400" : "bg-amber-400"}`} />
                <span className={`relative inline-flex rounded-full h-3 w-3 ${allHealthy ? "bg-emerald-400" : "bg-amber-400"}`} />
              </span>
              <span className="font-display font-semibold">{allHealthy ? "All systems operational" : "Degraded — some providers cooling down"}</span>
            </div>
            <button onClick={load} className="flex items-center gap-1.5 text-xs text-white/50 hover:text-white/80 transition-colors">
              <RefreshCw size={13} className={ticking ? "animate-spin" : ""} /> Refresh
            </button>
          </div>

          {/* Metric tiles */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {stat(Zap, "LLM calls served", metrics ? String(metrics.total_llm_calls) : "—", "bg-brand-500/15 text-brand-300")}
            {stat(Clock, "Uptime", metrics ? fmtUptime(metrics.uptime_seconds) : "—", "bg-cyanx-400/15 text-cyanx-300")}
            {stat(Database, "Jobs stored", metrics ? String(metrics.jobs_stored) : "—", "bg-iris-500/15 text-iris-400")}
            {stat(Cpu, "Providers", String(providers.length), "bg-emerald-500/15 text-emerald-400")}
          </div>

          {/* Provider cards */}
          <div className="grid sm:grid-cols-2 gap-4">
            {providers.map((p) => {
              const meta = PROVIDER_META[p.name] || { label: p.name, emoji: "🤖" };
              const pct = p.total_keys ? Math.round((p.available_keys / p.total_keys) * 100) : 0;
              return (
                <div key={p.name} className="glass rounded-2xl p-5 card-hover">
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2.5">
                      <span className="text-xl">{meta.emoji}</span>
                      <span className="font-display font-semibold">{meta.label}</span>
                    </div>
                    <span className={`text-[11px] px-2 py-0.5 rounded-full border ${p.healthy ? "text-emerald-300 border-emerald-500/30 bg-emerald-500/10" : "text-amber-300 border-amber-500/30 bg-amber-500/10"}`}>
                      {p.healthy ? "healthy" : "cooling"}
                    </span>
                  </div>
                  <div className="h-2 rounded-full bg-white/8 overflow-hidden">
                    <div className="h-full rounded-full bg-brand-gradient transition-all duration-700" style={{ width: `${pct}%` }} />
                  </div>
                  <div className="flex justify-between mt-2 text-xs text-white/60">
                    <span>{p.available_keys}/{p.total_keys} keys available</span>
                    <span>{p.total_calls} calls</span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </Page>
  );
}
