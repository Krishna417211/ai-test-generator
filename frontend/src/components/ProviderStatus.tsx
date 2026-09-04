import { useEffect, useState } from "react";
import { Zap, AlertCircle, CheckCircle } from "lucide-react";
import { getProviderStatus } from "../utils/api";

interface Props {
  currentProvider?: string;
}

// Display names for the provider badge. These MUST be kept in step with the
// MODELS table in backend/services/llm_router.py by hand — nothing links them,
// and all three had drifted to models the app hadn't run in months (Gemini 1.5
// Flash, LLaMA 3.1, and a Claude Haiku that was two majors old), so the badge
// confidently named a model that never touched the request. The results screen
// takes the model it reports from the provenance record instead, which is the
// one that actually answered; this is only the "who is up" indicator.
const PROVIDER_LABELS: Record<string, string> = {
  gemini: "Gemini 3.6 Flash",
  groq: "Groq GPT-OSS 120B",
  claude: "Claude Haiku 4.5",
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
    <div className="rounded-xl border border-grey-700 bg-white/5 p-4">
      <div className="flex items-center gap-2 mb-3">
        <Zap size={14} className="text-grey-400" />
        <span className="text-xs font-semibold text-grey-400 uppercase tracking-wider">LLM Providers</span>
      </div>

      {/* The server already sends a full sentence ("Streaming from groq..."),
          so prefixing "Using" here read as "Using Streaming from groq...". */}
      {currentProvider && (
        <div className="mb-3 px-3 py-2 rounded-lg bg-white/[0.06] border border-grey-600 text-xs text-grey-300 flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-brand-400 animate-pulse shrink-0" />
          <span className="truncate">{currentProvider}</span>
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
              <span className="text-xs text-grey-300">
                {PROVIDER_LABELS[p.name] || p.name}
              </span>
            </div>
            <span className="text-xs text-grey-500">
              {p.available_keys}/{p.total_keys} keys
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
