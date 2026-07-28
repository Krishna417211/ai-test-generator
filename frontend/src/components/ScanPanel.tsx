import { useState, useEffect } from "react";
import { ShieldCheck, Loader2, Globe, AlertTriangle, CheckCircle2, Youtube, Crosshair } from "lucide-react";
import { scanUrl, getScanCapabilities, type ScanCapabilities } from "../utils/api";
import FlowPipeline, { applyStep, type StepStates } from "./FlowPipeline";
import TrustPanel from "./TrustPanel";
import { pluralize } from "../utils/format";
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
      {/* The detail panel is a sibling of this button, not a child of it: it
          now contains a link, and an <a> inside a <button> is invalid HTML —
          the click would toggle the card as well as follow the link. */}
      <button
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="w-full flex items-start gap-3 text-left"
      >
        <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${s.dot}`} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-[10px] font-bold uppercase tracking-wider ${s.text}`}>{s.label}</span>
            <span className="text-[10px] text-grey-500 uppercase tracking-wider">{f.category}</span>
          </div>
          <div className="text-sm text-white/90 font-medium mt-0.5">{f.title}</div>
        </div>
        <span className="text-xs text-grey-500 shrink-0">{open ? "−" : "+"}</span>
      </button>

      {open && (
        <div className="mt-2 space-y-2 text-xs pl-5">
          <p className="text-grey-300 leading-relaxed">{f.description}</p>
          {f.evidence && (
            <div className="font-mono text-[11px] text-grey-500 bg-black/30 rounded px-2 py-1 break-all">
              {f.evidence}
            </div>
          )}
          <div className="rounded-lg bg-emerald-500/8 border border-emerald-500/20 px-3 py-2">
            <div className="text-emerald-300 font-semibold mb-0.5">Fix</div>
            <p className="text-grey-300 leading-relaxed">{f.remediation}</p>
            {f.video_url && (
              // Labelled as a search, not "the" video: it opens YouTube results
              // for this issue. Promising a specific video would mean shipping
              // hard-coded ids that rot, or model-invented ones that never
              // existed. Saying what the link does costs nothing and is true.
              <a
                href={f.video_url}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-2 inline-flex items-center gap-1.5 text-[11px] text-rose-300 hover:text-rose-200 transition-colors"
              >
                <Youtube size={12} className="shrink-0" />
                Search YouTube for how to fix this
              </a>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function ScanPanel() {
  const [url, setUrl] = useState("");
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ScanResult | null>(null);
  const [steps, setSteps] = useState<StepStates>({});
  // Active scanning sends real attack traffic, so it's opt-in AND gated on an
  // explicit ownership confirmation — the toggle can't be armed without it.
  const [active, setActive] = useState(false);
  const [owns, setOwns] = useState(false);
  // Whether the server can actually run an active scan. Checked on mount so the
  // answer is on screen before the user commits to a scan — the alternative is
  // learning it from a scan_note after waiting for a scan that quietly ran
  // passive instead.
  const [caps, setCaps] = useState<ScanCapabilities | null>(null);
  useEffect(() => { getScanCapabilities().then(setCaps).catch(() => {}); }, []);

  const run = async () => {
    if (!url.trim()) return;
    const wantActive = active && owns;
    setScanning(true);
    setError(null);
    setResult(null);
    setSteps({});
    try {
      setResult(await scanUrl(url.trim(), (e) => setSteps((s) => applyStep(s, e)),
        { active: wantActive, authorized: wantActive }));
    } catch (e: any) {
      // The pipeline stays on screen on failure — the step still spinning is
      // where it broke, which beats an error message with no context.
      setError(e.message || "Scan failed");
    } finally {
      setScanning(false);
    }
  };

  const order: Severity[] = ["critical", "high", "medium", "low", "info"];

  return (
    <div className="space-y-4">
      {/* The page subtitle already lists what gets checked; repeating it here
          just made the reader parse the same sentence twice. Keep only what
          they can't know from it — that this is safe to point at production. */}
      {/* Says up front that this is for your own site. The server refuses
          third-party targets with a clear message either way, but finding that
          out after a failed scan is a worse way to learn it than a line of
          copy — and it's the same sentence in both places. */}
      <p className="text-sm text-grey-400 leading-relaxed">
        Point it at a site you own. The default scan is passive — it inspects
        configuration and never sends attack traffic. An active scan (below) goes
        further and probes for exploitable bugs.
      </p>

      <div className="relative">
        <Globe size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-500" />
        <input
          aria-label="URL to scan"
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="https://your-app.com"
          className="w-full pl-10 pr-4 py-3.5 rounded-xl bg-black/30 border border-grey-700 text-white placeholder:text-grey-400 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm"
        />
      </div>

      {/* Active scan opt-in. The toggle arms it; the confirmation below is what
          actually authorises the traffic, and the run() gate requires both. */}
      <div className="rounded-xl border border-grey-700 bg-black/20 p-3.5 space-y-2.5">
        <label className="flex items-start gap-3 cursor-pointer">
          <input
            type="checkbox"
            checked={active}
            onChange={(e) => { setActive(e.target.checked); if (!e.target.checked) setOwns(false); }}
            className="mt-0.5 accent-brand-500 w-4 h-4 shrink-0"
          />
          <span className="min-w-0">
            <span className="flex items-center gap-1.5 text-sm font-medium text-white/90">
              <Crosshair size={13} className="text-amber-400 shrink-0" />
              Active scan — probe for exploitable bugs (XSS, injection)
            </span>
            <span className="block text-xs text-grey-400 mt-0.5 leading-relaxed">
              Sends real attack traffic via OWASP ZAP. Slower, and only for a site
              you control.
            </span>
          </span>
        </label>
        {/* Named, not hidden: the checkbox stays usable (the scan still runs, as
            a passive audit, and says so), but nobody is left guessing why an
            active scan came back looking passive.
            Only when the server actually answered. A failed check ("code":
            "unknown" — API restarting, session expired, offline) says nothing
            about whether ZAP is up, and announcing it as "active scanning is
            unavailable" states as fact something we did not learn. The scan
            itself reports what really happened. */}
        {caps && caps.code !== "unknown" && !caps.active_available && (
          <div className="flex items-start gap-2 text-xs text-amber-200/80 leading-relaxed pl-1">
            <AlertTriangle size={12} className="mt-0.5 shrink-0" />
            <span>Active scanning is unavailable right now — {caps.reason}. Ticking it still runs the passive audit.</span>
          </div>
        )}
        {active && (
          <label className="flex items-start gap-3 cursor-pointer pl-1">
            <input
              type="checkbox"
              checked={owns}
              onChange={(e) => setOwns(e.target.checked)}
              className="mt-0.5 accent-amber-500 w-4 h-4 shrink-0"
            />
            <span className="text-xs text-amber-200/90 leading-relaxed">
              I confirm I own this site or am authorized to actively test it.
            </span>
          </label>
        )}
      </div>

      <button
        onClick={run}
        disabled={scanning || !url.trim() || (active && !owns)}
        className="w-full flex items-center justify-center gap-2 py-3.5 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-sm"
      >
        {scanning ? (
          <><Loader2 size={16} className="animate-spin" /> Scanning...</>
        ) : active ? (
          <><Crosshair size={16} /> Run active vulnerability scan</>
        ) : (
          <><ShieldCheck size={16} /> Scan for vulnerabilities</>
        )}
      </button>

      {/* Shown while it runs, and kept up if it fails so the failed step stays
          visible. Dropped once the result renders — by then the findings are
          the story, not how they were gathered. */}
      {(scanning || (error && Object.keys(steps).length > 0)) && (
        <FlowPipeline flow="scan" states={steps} title="Auditing your site" />
      )}

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
              <div className="flex items-center gap-2 flex-wrap">
                <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${
                  result.mode === "active"
                    ? "text-amber-300 bg-amber-500/15 border border-amber-500/30"
                    : "text-grey-400 bg-white/5 border border-white/10"
                }`}>
                  {result.mode === "active" ? "Active scan" : "Passive scan"}
                </span>
                <span className="text-xs text-grey-500 font-mono break-all">{result.final_url}</span>
              </div>
              <p className="text-sm text-grey-300 mt-1 leading-relaxed">{result.summary}</p>
            </div>
          </div>

          {/* An honest heads-up when active was asked for but couldn't run. */}
          {result.scan_note && (
            <div className="flex items-start gap-2 px-3.5 py-2.5 rounded-xl bg-amber-500/10 border border-amber-500/25 text-xs text-amber-200/90 leading-relaxed">
              <AlertTriangle size={13} className="mt-0.5 shrink-0" />
              {result.scan_note}
            </div>
          )}

          {/* No grounding for scan: the checks are deterministic — a header is
              present or it isn't — so there's no rate to quote and inventing one
              would be noise. What does vary is whether a model wrote the plan. */}
          <TrustPanel summarySource={result.summary_source} provenance={result.provenance} />

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
                {pluralize(result.findings.length, "finding")} · tap any to see details & the fix
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
