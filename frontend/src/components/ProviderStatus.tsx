import { useEffect, useState } from "react";
import { Zap, AlertCircle, CheckCircle } from "lucide-react";
import { getProviderStatus } from "../utils/api";

interface Props {
  currentProvider?: string;
}

const PROVIDER_LABELS: Record<string, string> = {
  gemini: "Gemini 1.5 Flash",
  groq: "Groq LLaMA 3.1",
  claude: "Claude Haiku",
};

export default function ProviderStatus({ currentProvider }: Props) {
  const [status, setStatus] = useState<any>(null);

  useEffect(() => {
    getProviderStatus().then(setStatus).catch(() => {});
    const interval = setInterval(() => {
      getProviderStatus().then(setStatus).catch(() => {});
    }, 10000);
    return () => clearInterval(interval);
  }, []);

  if (!status) return null;

  return (
    <div className="rounded-xl border border-white/10 bg-white/5 p-4">
      <div className="flex items-center gap-2 mb-3">
        <Zap size={14} className="text-progress-400" />
        <span className="text-xs font-semibold text-white/60 uppercase tracking-wider">LLM Providers</span>
      </div>

      {currentProvider && (
        <div className="mb-3 px-3 py-2 rounded-lg bg-progress-500/20 border border-progress-500/30 text-xs text-progress-300 flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-progress-400 animate-pulse" />
          Using {currentProvider}
        </div>
      )}

      <div className="space-y-2">
        {status.providers?.map((p: any) => (
          <div key={p.name} className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              {p.healthy ? (
                <CheckCircle size={12} className="text-emerald-400" />
              ) : (
                <AlertCircle size={12} className="text-rose-400" />
              )}
              <span className="text-xs text-white/70">
                {PROVIDER_LABELS[p.name] || p.name}
              </span>
            </div>
            <span className="text-xs text-white/40">
              {p.available_keys}/{p.total_keys} keys
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
