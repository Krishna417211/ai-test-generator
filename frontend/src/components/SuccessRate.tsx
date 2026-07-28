import { useState } from "react";
import { Gauge, ChevronDown, Info } from "lucide-react";
import type { SuccessRate as SuccessRateData } from "../types";

/**
 * The headline number: what share of our automated checks this suite passed.
 *
 * Two things about it are deliberate and load-bearing.
 *
 * It is labelled a *generation* success rate, not "accuracy". We never execute
 * the generated tests — that needs Docker and a live target — so a number
 * claiming a pass rate would be invented, and the failure mode of inventing one
 * isn't a slightly wrong figure: the user trusts "94% accurate", runs the
 * suite, watches it fail, and never comes back. The caveat under the number
 * comes from the server (`not_measured`) so it cannot be dropped by editing
 * this file alone.
 *
 * And the breakdown is one click away, always. A single number that can't be
 * interrogated is a number you either believe or don't; the components say
 * exactly which checks ran, how many passed, and what a shortfall means.
 */

interface Props {
  data?: SuccessRateData | null;
  className?: string;
}

/** Tone by band. Amber, never red: a suite scoring 60 is "read this before you
 *  rely on it", not "this is broken" — it usually means the repo has few stable
 *  selectors, which is a fact about the repo. */
function tone(score: number) {
  if (score >= 90) return { text: "text-brand-300", ring: "stroke-brand-400", bg: "bg-brand-400/10" };
  if (score >= 70) return { text: "text-grey-100", ring: "stroke-grey-400", bg: "bg-white/[0.06]" };
  return { text: "text-amber-300", ring: "stroke-amber-400", bg: "bg-amber-400/10" };
}

function Dial({ score }: { score: number }) {
  const t = tone(score);
  const r = 26;
  const circumference = 2 * Math.PI * r;
  const dash = (score / 100) * circumference;

  return (
    <div className="relative shrink-0" style={{ width: 64, height: 64 }}>
      <svg width="64" height="64" viewBox="0 0 64 64" className="-rotate-90">
        <circle cx="32" cy="32" r={r} fill="none" strokeWidth="5"
          className="stroke-white/[0.08]" />
        <circle cx="32" cy="32" r={r} fill="none" strokeWidth="5"
          strokeLinecap="round" className={t.ring}
          strokeDasharray={`${dash} ${circumference}`} />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center">
        <span className={`text-lg font-semibold tabular-nums ${t.text}`}>{score}</span>
      </div>
    </div>
  );
}

function ComponentRow({ label, passed, total, detail }: {
  label: string; passed: number; total: number; detail: string;
}) {
  const pct = total > 0 ? Math.round((passed / total) * 100) : 0;
  const complete = passed === total;
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs text-grey-400">{label}</span>
        <span className={`text-xs font-mono ${complete ? "text-brand-300" : "text-amber-300"}`}>
          {passed}/{total}
          <span className="text-grey-600"> · {pct}%</span>
        </span>
      </div>
      {!complete && detail && (
        <p className="text-[11px] text-grey-500 leading-relaxed pl-0.5">{detail}</p>
      )}
    </div>
  );
}

export default function SuccessRate({ data, className = "" }: Props) {
  const [open, setOpen] = useState(false);

  // Nothing was measurable. Rendering a 0 here would read as a failing suite
  // when the truth is that no check had anything to run against.
  if (!data || !data.measured || data.score === null || data.score === undefined) return null;

  const t = tone(data.score);

  return (
    <div className={`rounded-xl border border-grey-700 bg-white/[0.02] overflow-hidden ${className}`}>
      <div className="p-4 flex items-start gap-4">
        <Dial score={data.score} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Gauge size={13} className="text-grey-500 shrink-0" />
            <span className="text-sm font-semibold text-white">Generation success rate</span>
            <span className={`text-xs px-1.5 py-0.5 rounded border border-grey-600 ${t.bg} ${t.text}`}>
              {data.grade}
            </span>
          </div>
          <p className="text-xs text-grey-500 mt-1.5 leading-relaxed">{data.measures}</p>
          <button
            onClick={() => setOpen((v) => !v)}
            className="mt-2 flex items-center gap-1 text-xs text-grey-400 hover:text-grey-200 transition-colors"
          >
            {open ? "Hide" : "See"} the {data.components.length} checks
            <ChevronDown size={12} className={`transition-transform ${open ? "rotate-180" : ""}`} />
          </button>
        </div>
      </div>

      {open && (
        <div className="px-4 pb-4 space-y-2.5 border-t border-grey-700/60 pt-3">
          {data.components.map((c) => (
            <ComponentRow key={c.key} label={c.label} passed={c.passed}
              total={c.total} detail={c.detail} />
          ))}
        </div>
      )}

      {/* The sentence that keeps the number honest. Server-supplied, and shown
          whether or not the breakdown is expanded. */}
      <p className="px-4 pb-3.5 text-[11px] text-grey-500 leading-relaxed flex gap-1.5">
        <Info size={11} className="shrink-0 mt-0.5 text-grey-600" />
        <span>{data.not_measured}</span>
      </p>
    </div>
  );
}
