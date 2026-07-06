import { ChevronRight, Layers, AlertTriangle, Route } from "lucide-react";
import type { ProjectAnalysis } from "../types";

interface Props {
  analysis: ProjectAnalysis;
  onContinue: () => void;
}

const PRIORITY_COLORS = {
  high: "text-red-400 bg-red-400/10 border-red-400/20",
  medium: "text-amber-400 bg-amber-400/10 border-amber-400/20",
  low: "text-emerald-400 bg-emerald-400/10 border-emerald-400/20",
};

export default function PreviewStep({ analysis, onContinue }: Props) {
  return (
    <div className="w-full max-w-3xl mx-auto space-y-5">
      {/* Summary card */}
      <div className="rounded-xl border border-white/10 bg-white/5 p-5">
        <div className="flex items-start justify-between mb-3">
          <div>
            <span className="text-xs font-semibold text-violet-400 uppercase tracking-wider">
              {analysis.framework}
            </span>
            <h3 className="text-base font-semibold text-white mt-1">Project Analysis</h3>
          </div>
          <div className="text-right">
            <div className="text-2xl font-bold text-white">{analysis.file_count}</div>
            <div className="text-xs text-white/40">UI files found</div>
          </div>
        </div>
        <p className="text-sm text-white/60 leading-relaxed">{analysis.project_summary}</p>
        <div className="mt-3 text-xs text-white/30">
          ~{(analysis.total_tokens / 1000).toFixed(0)}k tokens • {analysis.routes.length} routes detected
        </div>
      </div>

      {/* Routes */}
      {analysis.routes.length > 0 && (
        <div className="rounded-xl border border-white/10 bg-white/5 p-5">
          <div className="flex items-center gap-2 mb-3">
            <Route size={14} className="text-violet-400" />
            <span className="text-sm font-semibold text-white">Detected Routes</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {analysis.routes.map((route) => (
              <span
                key={route}
                className="px-2.5 py-1 rounded-lg bg-white/5 border border-white/10 text-xs text-white/60 font-mono"
              >
                {route}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Key pages */}
      {analysis.key_pages.length > 0 && (
        <div className="rounded-xl border border-white/10 bg-white/5 p-5">
          <div className="flex items-center gap-2 mb-4">
            <Layers size={14} className="text-violet-400" />
            <span className="text-sm font-semibold text-white">Key Pages to Test</span>
          </div>
          <div className="space-y-3">
            {analysis.key_pages.map((page, i) => (
              <div key={i} className="flex gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs font-mono text-violet-300 truncate">{page.path}</span>
                    <span
                      className={`shrink-0 px-1.5 py-0.5 rounded text-xs border ${
                        PRIORITY_COLORS[page.test_priority as keyof typeof PRIORITY_COLORS] ||
                        PRIORITY_COLORS.medium
                      }`}
                    >
                      {page.test_priority}
                    </span>
                  </div>
                  <p className="text-xs text-white/50">{page.description}</p>
                  {page.suggested_tests?.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {page.suggested_tests.map((t, j) => (
                        <span key={j} className="text-xs text-white/30 bg-white/5 px-2 py-0.5 rounded">
                          {t}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Warnings */}
      {analysis.testing_challenges.length > 0 && (
        <div className="rounded-xl border border-amber-500/20 bg-amber-500/5 p-5">
          <div className="flex items-center gap-2 mb-3">
            <AlertTriangle size={14} className="text-amber-400" />
            <span className="text-sm font-semibold text-amber-300">Testing Considerations</span>
          </div>
          <ul className="space-y-1.5">
            {analysis.testing_challenges.map((c, i) => (
              <li key={i} className="text-xs text-amber-200/70 flex gap-2">
                <span className="shrink-0 mt-0.5">•</span>
                {c}
              </li>
            ))}
          </ul>
        </div>
      )}

      <button
        onClick={onContinue}
        className="w-full flex items-center justify-center gap-2 py-3.5 rounded-xl bg-violet-600 hover:bg-violet-500 text-white font-semibold transition-all text-sm"
      >
        Configure Test Generation
        <ChevronRight size={16} />
      </button>
    </div>
  );
}
