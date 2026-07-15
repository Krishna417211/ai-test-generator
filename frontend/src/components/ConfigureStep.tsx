import { useState } from "react";
import { Play, Settings, AlertTriangle } from "lucide-react";
import type { Framework, Language } from "../types";

interface Config {
  framework: Framework;
  language: Language;
  testFlows: string;
  baseUrl: string;
  includeCi: boolean;
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
        onClick={() => onGenerate({ framework, language, testFlows, baseUrl, includeCi })}
        disabled={loading}
        className="w-full flex items-center justify-center gap-2 py-4 rounded-xl btn-primary disabled:opacity-40 font-semibold"
      >
        <Play size={16} />
        {loading ? "Generating..." : "Generate Test Suite"}
      </button>
    </div>
  );
}
