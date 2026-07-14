import { useState } from "react";
import { Play, Settings } from "lucide-react";
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

export default function ConfigureStep({ detectedFramework, onGenerate, loading }: Props) {
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
        <span>Detected: <span className="text-progress-400">{detectedFramework}</span></span>
      </div>

      {/* Framework */}
      <div>
        <label className="block text-sm font-semibold text-white mb-3">Test Framework</label>
        <div className="grid grid-cols-3 gap-3">
          {FRAMEWORKS.map((fw) => (
            <button
              key={fw.id}
              onClick={() => handleFrameworkChange(fw.id)}
              className={`p-3 rounded-xl border text-left transition-all ${
                framework === fw.id
                  ? "border-progress-500 bg-progress-500/15 text-white"
                  : "border-grey-700 bg-white/5 text-grey-400 hover:border-white/20 hover:text-grey-300"
              }`}
            >
              <div className="font-semibold text-sm">{fw.label}</div>
              <div className="text-xs mt-1 opacity-60">{fw.desc}</div>
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
              className={`px-4 py-2 rounded-lg border text-sm font-medium transition-all ${
                language === lang.id
                  ? "border-progress-500 bg-progress-500/15 text-white"
                  : "border-grey-700 bg-white/5 text-grey-400 hover:text-grey-300"
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
          className="w-full px-4 py-3 rounded-xl bg-white/5 border border-grey-700 text-white placeholder:text-white/25 focus:outline-none focus:border-progress-500 transition-colors text-sm resize-none"
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
          className="w-full px-4 py-3 rounded-xl bg-white/5 border border-grey-700 text-white focus:outline-none focus:border-progress-500 transition-colors text-sm font-mono"
        />
      </div>

      {/* CI toggle */}
      <label className="flex items-center gap-3 cursor-pointer select-none">
        <div
          onClick={() => setIncludeCi(!includeCi)}
          className={`w-10 h-6 rounded-full border transition-all ${
            includeCi ? "bg-progress-600 border-progress-500" : "bg-white/10 border-white/20"
          } relative`}
        >
          <span
            className={`absolute top-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
              includeCi ? "left-4" : "left-0.5"
            }`}
          />
        </div>
        <span className="text-sm text-grey-300">
          Include GitHub Actions + GitLab CI yaml
        </span>
      </label>

      <button
        onClick={() => onGenerate({ framework, language, testFlows, baseUrl, includeCi })}
        disabled={loading}
        className="w-full flex items-center justify-center gap-2 py-4 rounded-xl bg-progress-600 hover:bg-progress-500 disabled:opacity-40 text-white font-bold transition-all"
      >
        <Play size={16} />
        Generate Test Suite
      </button>
    </div>
  );
}
