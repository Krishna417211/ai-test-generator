import { useState } from "react";
import {
  Play, Settings, AlertTriangle, Globe, Loader2, CheckCircle2, XCircle, Ban,
} from "lucide-react";
import type { Framework, Language } from "../types";
import { previewCrawl, type CrawlPreview } from "../utils/api";

interface Config {
  framework: Framework;
  language: Language;
  testFlows: string;
  baseUrl: string;
  includeCi: boolean;
  /** Optional deployed URL the user owns. When set, generated selectors are
   *  verified against the live DOM and self-healed if they miss. */
  liveUrl: string;
}

interface Props {
  detectedFramework: string;
  onGenerate: (config: Config) => void;
  loading: boolean;
  /** A failed generation returns the user to this step. Without surfacing the
   *  reason here, a 503 looked identical to the button doing nothing at all. */
  error?: string | null;
}

const FRAMEWORKS: { id: Framework; label: string; desc: string }[] = [
  { id: "playwright", label: "Playwright", desc: "Fast, reliable, supports all browsers" },
  { id: "cypress", label: "Cypress", desc: "Great DX, real-time browser preview" },
  { id: "selenium", label: "Selenium", desc: "Industry standard, broad language support" },
];

const LANGUAGES: Record<Framework, { id: Language; label: string }[]> = {
  playwright: [
    { id: "typescript", label: "TypeScript" },
    { id: "javascript", label: "JavaScript" },
    { id: "python", label: "Python" },
  ],
  cypress: [
    { id: "typescript", label: "TypeScript" },
    { id: "javascript", label: "JavaScript" },
  ],
  selenium: [
    { id: "python", label: "Python" },
    { id: "java", label: "Java" },
    { id: "javascript", label: "JavaScript" },
  ],
};

export default function ConfigureStep({ detectedFramework, onGenerate, loading, error }: Props) {
  const [framework, setFramework] = useState<Framework>("playwright");
  const [language, setLanguage] = useState<Language>("typescript");
  const [testFlows, setTestFlows] = useState("");
  const [baseUrl, setBaseUrl] = useState("http://localhost:3000");
  const [includeCi, setIncludeCi] = useState(true);
  const [liveUrl, setLiveUrl] = useState("");

  // "Preview crawl" — run the live crawler on liveUrl by itself so the user can
  // confirm it actually renders their site before spending a generation credit.
  const [crawl, setCrawl] = useState<CrawlPreview | null>(null);
  const [crawling, setCrawling] = useState(false);
  const [crawlError, setCrawlError] = useState<string | null>(null);

  const runPreview = async () => {
    const url = liveUrl.trim();
    if (!url || crawling) return;
    setCrawling(true);
    setCrawlError(null);
    setCrawl(null);
    try {
      setCrawl(await previewCrawl(url));
    } catch (e) {
      setCrawlError(e instanceof Error ? e.message : "Crawl preview failed");
    } finally {
      setCrawling(false);
    }
  };

  const handleFrameworkChange = (fw: Framework) => {
    setFramework(fw);
    const langs = LANGUAGES[fw];
    if (!langs.find((l) => l.id === language)) {
      setLanguage(langs[0].id);
    }
  };

  return (
    <div className="w-full max-w-2xl mx-auto space-y-6">
      <div className="flex items-center gap-2 text-grey-500 text-xs mb-2">
        <Settings size={13} />
        <span>Detected: <span className="text-grey-300">{detectedFramework}</span></span>
      </div>

      {/* Framework */}
      <div>
        <label className="block text-sm font-semibold text-white mb-3">Test Framework</label>
        <div className="grid grid-cols-3 gap-3">
          {FRAMEWORKS.map((fw) => (
            <button
              key={fw.id}
              onClick={() => handleFrameworkChange(fw.id)}
              aria-pressed={framework === fw.id}
              className={`p-3 rounded-xl border text-left transition-all ${
                framework === fw.id
                  ? "border-grey-300 bg-white/[0.08] text-white shadow-inner-hi"
                  : "border-grey-700 bg-white/[0.02] text-grey-400 hover:border-grey-600 hover:text-grey-300"
              }`}
            >
              <div className="font-semibold text-sm">{fw.label}</div>
              <div className={`text-xs mt-1 ${framework === fw.id ? "text-grey-400" : "text-grey-500"}`}>
                {fw.desc}
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Language */}
      <div>
        <label className="block text-sm font-semibold text-white mb-3">Language</label>
        <div className="flex gap-2">
          {LANGUAGES[framework].map((lang) => (
            <button
              key={lang.id}
              onClick={() => setLanguage(lang.id)}
              aria-pressed={language === lang.id}
              className={`px-4 py-2 rounded-lg border text-sm font-medium transition-all ${
                language === lang.id
                  ? "border-grey-300 bg-white/[0.08] text-white shadow-inner-hi"
                  : "border-grey-700 bg-white/[0.02] text-grey-400 hover:border-grey-600 hover:text-grey-300"
              }`}
            >
              {lang.label}
            </button>
          ))}
        </div>
      </div>

      {/* Test flows */}
      <div>
        <label className="block text-sm font-semibold text-white mb-2">
          What to test <span className="text-grey-500 font-normal">(optional)</span>
        </label>
        <textarea
          value={testFlows}
          onChange={(e) => setTestFlows(e.target.value)}
          placeholder={`Describe the flows you want tested. For example:\n- User registration and login\n- Product search and add to cart\n- Checkout with payment form\n- Profile settings update`}
          rows={5}
          className="w-full px-4 py-3 rounded-xl bg-black/20 border border-grey-700 text-white placeholder:text-grey-500 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm resize-none"
        />
        <p className="text-xs text-grey-500 mt-1">
          Leave blank to auto-test all detected pages and flows.
        </p>
      </div>

      {/* Base URL */}
      <div>
        <label className="block text-sm font-semibold text-white mb-2">Base URL</label>
        <input
          type="url"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          className="w-full px-4 py-3 rounded-xl bg-black/20 border border-grey-700 text-white focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm font-mono"
        />
      </div>

      {/* Live URL — optional. Its presence is what upgrades grounding from
          "selectors exist in your source" to "selectors match your live site",
          and turns on the self-heal loop for the ones that don't. */}
      <div>
        <label className="block text-sm font-semibold text-white mb-2">
          Live site URL <span className="font-normal text-grey-500">— optional</span>
        </label>
        <input
          type="url"
          value={liveUrl}
          onChange={(e) => setLiveUrl(e.target.value)}
          placeholder="https://your-app.com"
          className="w-full px-4 py-3 rounded-xl bg-black/20 border border-grey-700 text-white placeholder:text-grey-500 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm font-mono"
        />
        <p className="text-xs text-grey-500 mt-1">
          If it&apos;s deployed and you own it, we&apos;ll verify every selector against the live
          DOM and auto-fix the ones that don&apos;t match. Nothing is executed — we only read the page.
        </p>

        {/* Preview crawl — exercises the live crawler on its own so you can see
            it working (routes rendered, anchors indexed) before generating. */}
        <div className="mt-3">
          <button
            type="button"
            onClick={runPreview}
            disabled={!liveUrl.trim() || crawling}
            className="inline-flex items-center gap-2 px-3.5 py-2 rounded-lg border border-grey-700 bg-white/[0.02] text-sm text-grey-300 hover:border-grey-600 hover:text-white disabled:opacity-40 disabled:hover:border-grey-700 transition-all"
          >
            {crawling ? <Loader2 size={14} className="animate-spin" /> : <Globe size={14} />}
            {crawling ? "Crawling your site…" : "Preview live crawl"}
          </button>
          <p className="text-xs text-grey-500 mt-1.5">
            Renders your site and follows its own links — the same crawl used for grounding. No credit spent.
          </p>

          {crawlError && (
            <div className="mt-3 flex items-start gap-2.5 px-4 py-3 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300">
              <AlertTriangle size={15} className="shrink-0 mt-0.5" />
              <span>{crawlError}</span>
            </div>
          )}

          {crawl && <CrawlResultCard crawl={crawl} />}
        </div>
      </div>

      {/* CI toggle — a real switch, so the whole row (not just the 40px track)
          is a hit target and screen readers get the on/off state. */}
      <button
        type="button"
        role="switch"
        aria-checked={includeCi}
        onClick={() => setIncludeCi(!includeCi)}
        className="flex items-center gap-3 select-none group"
      >
        <span
          className={`w-10 h-6 rounded-full border transition-colors relative shrink-0 ${
            includeCi ? "bg-brand-500 border-brand-400" : "bg-white/5 border-grey-600"
          }`}
        >
          <span
            className={`absolute top-0.5 w-5 h-5 rounded-full shadow transition-all ${
              includeCi ? "left-4 bg-ink-950" : "left-0.5 bg-grey-400"
            }`}
          />
        </span>
        <span className="text-sm text-grey-300 group-hover:text-white transition-colors">
          Include GitHub Actions + GitLab CI yaml
        </span>
      </button>

      {error && (
        <div className="flex items-start gap-2.5 px-4 py-3 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300">
          <AlertTriangle size={15} className="shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      <button
        onClick={() => onGenerate({ framework, language, testFlows, baseUrl, includeCi, liveUrl })}
        disabled={loading}
        className="w-full flex items-center justify-center gap-2 py-4 rounded-xl btn-primary disabled:opacity-40 font-semibold"
      >
        <Play size={16} />
        {loading ? "Generating..." : "Generate Test Suite"}
      </button>
    </div>
  );
}

// Per-status framing. "thin"/"unavailable" are not failures — the crawl ran (or
// couldn't run here) and a real generation would degrade safely — so they read
// amber, distinct from a hard red error, and each says what generation would do.
const STATUS: Record<
  CrawlPreview["status"],
  { icon: typeof Globe; label: string; ring: string; tint: string }
> = {
  ok:          { icon: CheckCircle2, label: "Crawl succeeded",   ring: "border-emerald-500/30 bg-emerald-500/[0.07]", tint: "text-emerald-300" },
  thin:        { icon: AlertTriangle, label: "Rendered, but thin", ring: "border-amber-500/30 bg-amber-500/[0.07]",   tint: "text-amber-300" },
  unavailable: { icon: Ban,          label: "Crawler unavailable", ring: "border-amber-500/30 bg-amber-500/[0.07]",   tint: "text-amber-300" },
  blocked:     { icon: Ban,          label: "URL blocked",        ring: "border-rose-500/30 bg-rose-500/[0.07]",     tint: "text-rose-300" },
  error:       { icon: XCircle,      label: "Crawl failed",       ring: "border-rose-500/30 bg-rose-500/[0.07]",     tint: "text-rose-300" },
};

/** Shows what the live crawler actually saw — routes rendered, anchors indexed,
 *  a sample of those anchors, and routes it discovered but didn't visit. This is
 *  the "is it working?" proof, right in the generate flow. */
function CrawlResultCard({ crawl }: { crawl: CrawlPreview }) {
  const s = STATUS[crawl.status];
  const Icon = s.icon;
  const ran = crawl.n_pages > 0;

  return (
    <div className={`mt-3 rounded-xl border ${s.ring} p-4 space-y-3`}>
      <div className="flex items-center justify-between gap-3">
        <span className={`flex items-center gap-2 text-sm font-semibold ${s.tint}`}>
          <Icon size={15} className="shrink-0" />
          {s.label}
        </span>
        <span className="text-[11px] font-mono text-grey-500">{crawl.elapsed_ms} ms</span>
      </div>

      {ran && (
        <div className="flex flex-wrap gap-4 text-sm">
          <span className="text-grey-300">
            <span className="font-mono text-white">{crawl.n_pages}</span>{" "}
            <span className="text-grey-500">route{crawl.n_pages === 1 ? "" : "s"} rendered</span>
          </span>
          <span className="text-grey-300">
            <span className="font-mono text-white">{crawl.n_anchors}</span>{" "}
            <span className="text-grey-500">anchor{crawl.n_anchors === 1 ? "" : "s"} indexed</span>
          </span>
        </div>
      )}

      {crawl.message && (
        <p className="text-xs text-grey-400 leading-relaxed">{crawl.message}</p>
      )}

      {crawl.pages.length > 0 && (
        <div>
          <div className="text-[11px] uppercase tracking-wide text-grey-500 mb-1.5">Routes rendered</div>
          <div className="max-h-40 overflow-y-auto rounded-lg border border-grey-800 divide-y divide-grey-800">
            {crawl.pages.map((p) => (
              <div key={p.path} className="flex items-center justify-between gap-3 px-3 py-1.5 text-xs">
                <span className="font-mono text-grey-300 truncate">{p.path || "/"}</span>
                <span className="font-mono text-grey-500 shrink-0">{p.n_anchors}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {crawl.sample_anchors.length > 0 && (
        <div>
          <div className="text-[11px] uppercase tracking-wide text-grey-500 mb-1.5">Sample anchors</div>
          <div className="flex flex-wrap gap-1.5">
            {crawl.sample_anchors.map((a, i) => (
              <span
                key={`${a.kind}:${a.value}:${i}`}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-white/[0.03] border border-grey-800 text-[11px]"
              >
                <span className="text-brand-300 font-mono">{a.kind}</span>
                <span className="text-grey-400 font-mono truncate max-w-[180px]">{a.value}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {crawl.unvisited.length > 0 && (
        <p className="text-[11px] text-grey-500">
          + {crawl.unvisited.length} more route{crawl.unvisited.length === 1 ? "" : "s"} discovered but not visited (page cap reached).
        </p>
      )}
    </div>
  );
}
