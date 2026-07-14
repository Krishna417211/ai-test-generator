import { useState } from "react";
import { ShieldCheck, Loader2, Globe, AlertTriangle, CheckCircle2 } from "lucide-react";
import { scanUrl } from "../utils/api";
import type { ScanResult, SecurityFinding, Severity } from "../types";

const SEV_STYLE: Record<Severity, { label: string; dot: string; text: string; ring: string }> = {
  critical: { label: "Critical", dot: "bg-rose-500", text: "text-rose-400", ring: "border-rose-500/30 bg-rose-500/10" },
  high: { label: "High", dot: "bg-rose-400", text: "text-rose-400", ring: "border-rose-400/30 bg-rose-400/10" },
  medium: { label: "Medium", dot: "bg-amber-400", text: "text-amber-400", ring: "border-amber-500/30 bg-amber-500/10" },
  low: { label: "Low", dot: "bg-iris-400", text: "text-iris-400", ring: "border-iris-400/30 bg-iris-400/10" },
  info: { label: "Info", dot: "bg-white/40", text: "text-grey-300", ring: "border-white/15 bg-white/5" },
};

const GRADE_COLOR: Record<string, string> = {
  A: "text-emerald-400 border-emerald-500/40 bg-emerald-500/10",
  B: "text-brand-400 border-brand-500/40 bg-brand-500/10",
  C: "text-amber-400 border-amber-500/40 bg-amber-500/10",
  D: "text-amber-500 border-amber-500/50 bg-amber-500/15",
  F: "text-rose-500 border-rose-500/40 bg-rose-500/10",
};

function FindingCard({ f }: { f: SecurityFinding }) {
  const s = SEV_STYLE[f.severity] ?? SEV_STYLE.info;
  const [open, setOpen] = useState(false);
  return (
    <div className={`rounded-xl border ${s.ring} p-3.5`}>
      <button onClick={() => setOpen(!open)} className="w-full flex items-start gap-3 text-left">
        <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${s.dot}`} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-[10px] font-bold uppercase tracking-wider ${s.text}`}>{s.label}</span>
            <span className="text-[10px] text-grey-500 uppercase tracking-wider">{f.category}</span>
          </div>
          <div className="text-sm text-white/90 font-medium mt-0.5">{f.title}</div>
          {open && (
            <div className="mt-2 space-y-2 text-xs">
              <p className="text-grey-300 leading-relaxed">{f.description}</p>
              {f.evidence && (
                <div className="font-mono text-[11px] text-grey-500 bg-black/30 rounded px-2 py-1 break-all">
                  {f.evidence}
                </div>
              )}
              <div className="rounded-lg bg-emerald-500/8 border border-emerald-500/20 px-3 py-2">
                <div className="text-emerald-300 font-semibold mb-0.5">Fix</div>
                <p className="text-grey-300 leading-relaxed">{f.remediation}</p>
              </div>
            </div>
          )}
        </div>
        <span className="text-xs text-grey-500 shrink-0">{open ? "−" : "+"}</span>
      </button>
    </div>
  );
}

export default function ScanPanel() {
  const [url, setUrl] = useState("");
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ScanResult | null>(null);

  const run = async () => {
    if (!url.trim()) return;
    setScanning(true);
    setError(null);
    setResult(null);
    try {
      setResult(await scanUrl(url.trim()));
    } catch (e: any) {
      setError(e.message || "Scan failed");
    } finally {
      setScanning(false);
    }
  };

  const order: Severity[] = ["critical", "high", "medium", "low", "info"];

  return (
    <div className="space-y-4">
      <p className="text-sm text-grey-300 leading-relaxed">
        Enter your deployed URL and we'll audit it for common production security issues —
        HTTPS/TLS, security headers, cookie flags, CORS, and accidentally-exposed files —
        with a concrete fix for each. Passive & non-intrusive: it inspects configuration,
        it doesn't attack your site.
      </p>

      <div className="relative">
        <Globe size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-500" />
        <input
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="https://your-app.com"
          className="w-full pl-10 pr-4 py-3.5 rounded-xl bg-black/30 border border-grey-700 text-white placeholder:text-grey-400 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm"
        />
      </div>

      <button
        onClick={run}
        disabled={scanning || !url.trim()}
        className="w-full flex items-center justify-center gap-2 py-3.5 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-sm"
      >
        {scanning ? (
          <><Loader2 size={16} className="animate-spin" /> Scanning...</>
        ) : (
          <><ShieldCheck size={16} /> Scan for vulnerabilities</>
        )}
      </button>

      {error && (
        <div className="px-4 py-3 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-400">
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-4">
          {/* Score header */}
          <div className="flex items-center gap-4 rounded-xl border border-grey-700 bg-white/5 p-4">
            <div className={`w-16 h-16 rounded-xl border flex flex-col items-center justify-center ${GRADE_COLOR[result.grade] || GRADE_COLOR.F}`}>
              <span className="text-2xl font-black leading-none">{result.grade}</span>
              <span className="text-[10px] opacity-70">{result.score}/100</span>
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-xs text-grey-500 font-mono break-all">{result.final_url}</div>
              <p className="text-sm text-grey-300 mt-1 leading-relaxed">{result.summary}</p>
            </div>
          </div>

          {/* Severity counts */}
          <div className="flex flex-wrap gap-2">
            {order.map((sev) =>
              (result.counts?.[sev] ?? 0) > 0 ? (
                <span key={sev} className={`text-xs px-2.5 py-1 rounded-full border ${SEV_STYLE[sev].ring} ${SEV_STYLE[sev].text}`}>
                  {result.counts[sev]} {SEV_STYLE[sev].label}
                </span>
              ) : null
            )}
          </div>

          {/* Findings */}
          {result.findings.length === 0 ? (
            <div className="flex items-center gap-2 px-4 py-3 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-sm text-emerald-300">
              <CheckCircle2 size={16} /> No issues detected by the passive checks.
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center gap-1.5 text-xs text-grey-500">
                <AlertTriangle size={12} />
                {result.findings.length} finding(s) · tap any to see details & the fix
              </div>
              {result.findings.map((f, i) => (
                <FindingCard key={i} f={f} />
              ))}
            </div>
          )}

          <p className="text-[11px] text-white/25 leading-relaxed">
            This is a lightweight passive scan of configuration — a clean result isn't a
            substitute for a full penetration test.
          </p>
        </div>
      )}
    </div>
  );
}
