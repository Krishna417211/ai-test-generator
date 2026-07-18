import { ShieldCheck, Brain, Info } from "lucide-react";
import type { Grounding, Provenance } from "../types";

/**
 * "How much can I trust this, and what wrote it?" — answered the same way for
 * all three flows.
 *
 * The deliberate omission here is an accuracy score. We never execute the
 * generated tests (that needs Docker and a live target — see
 * backend/services/validator.py), so a number claiming to predict whether they
 * pass would be invented. The failure mode of inventing one is not a slightly
 * wrong number: the user trusts "94% accurate", runs the suite, watches it
 * fail, and never comes back. So this reports the two things we genuinely
 * measure, under their own names, and says plainly what it didn't check.
 *
 * The model is named; the API key is not. Which of the numbered keys served a
 * call is an operations detail (admin console → key health) and would leak the
 * key pool's shape into screenshots for no user benefit.
 */

interface Props {
  grounding?: Grounding | null;
  provenance?: Provenance | null;
  /** Scan has no grounding — its checks are deterministic — but it does need to
   *  say whether a model wrote the plan or it fell back to the plain summary. */
  summarySource?: "ai" | "fallback";
  className?: string;
}

/** A measured fraction, or an explicit "nothing to measure".
 *
 *  `rate` is null when the denominator was zero, and that case must not render
 *  as 100%: a suite that referenced no selectors has not earned a perfect
 *  score, and claiming one would be the most confident lie on the page. */
function Stat({
  label,
  verified,
  total,
  rate,
  emptyNote,
}: {
  label: string;
  verified: number;
  total: number;
  rate: number | null;
  emptyNote: string;
}) {
  if (rate === null || total === 0) {
    return (
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs text-grey-400">{label}</span>
        <span className="text-xs text-grey-500 italic">{emptyNote}</span>
      </div>
    );
  }

  const pct = Math.round(rate * 100);
  // Amber, not red: an unverified selector is "check this", not "this is
  // broken" — the model may have used a selector our regex scan can't see.
  const tone = pct === 100 ? "text-brand-300" : pct >= 80 ? "text-grey-200" : "text-amber-300";

  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs text-grey-400">{label}</span>
      <span className={`text-xs font-mono ${tone}`}>
        {verified}/{total}
        <span className="text-grey-600"> · {pct}%</span>
      </span>
    </div>
  );
}

function ModelLine({ provenance }: { provenance: Provenance }) {
  const { primary, mixed, models, upgrade_model } = provenance;

  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs text-grey-400 flex items-center gap-1.5">
          <Brain size={11} className="text-grey-500" />
          {mixed ? "Mostly written by" : "Written by"}
        </span>
        <span className="text-xs font-mono text-grey-200">{primary.model}</span>
      </div>

      {/* Only when rotation actually split the work. Labelling a suite with one
          model when a second wrote part of it would be the same kind of small
          lie this panel exists to avoid. */}
      {mixed && (
        <p className="text-[11px] text-grey-500 leading-relaxed">
          Capacity rotated mid-run, so this came from{" "}
          {models.map((m, i) => (
            <span key={m.model}>
              {i > 0 && (i === models.length - 1 ? " and " : ", ")}
              <span className="font-mono text-grey-400">{m.model}</span>{" "}
              <span className="text-grey-600">({Math.round(m.share * 100)}%)</span>
            </span>
          ))}
          .
        </p>
      )}

      {/* Shown because the server says Pro routes to a different model — not
          because this run scored badly. Tying an upsell to a low score would
          give us a reason to want the score low, and a weak score usually means
          the repo lacks stable selectors, which a better model cannot invent. */}
      {upgrade_model && (
        <p className="text-[11px] text-grey-500 leading-relaxed">
          Pro runs <span className="font-mono text-grey-400">{upgrade_model}</span> on this step.
        </p>
      )}
    </div>
  );
}

export default function TrustPanel({ grounding, provenance, summarySource, className = "" }: Props) {
  // Nothing measured and nothing generated — render nothing rather than an
  // empty box implying we checked something.
  if (!grounding && !provenance && !summarySource) return null;

  return (
    <div className={`rounded-xl border border-grey-700 bg-white/[0.02] p-4 space-y-3 ${className}`}>
      <div className="flex items-center gap-2">
        <ShieldCheck size={13} className="text-grey-500" />
        <span className="text-xs font-semibold text-grey-300">What we checked</span>
      </div>

      {grounding && (
        <div className="space-y-2">
          <Stat
            label="Selectors found in your source"
            verified={grounding.selectors_verified}
            total={grounding.selectors_total}
            rate={grounding.selector_rate}
            emptyNote="no selectors to check"
          />
          <Stat
            label="Code files that parse"
            verified={grounding.files_valid}
            total={grounding.files_checked}
            rate={grounding.file_rate}
            emptyNote="nothing parseable"
          />
          {grounding.heal_attempts > 0 && (
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-xs text-grey-400">Auto-fix rounds used</span>
              <span className="text-xs font-mono text-grey-400">{grounding.heal_attempts}</span>
            </div>
          )}
        </div>
      )}

      {/* Scan's own trust signal. Its findings are deterministic — a header is
          present or it isn't — so there's no rate to quote here, only whether
          the prioritised plan is a model's or the plain deterministic one. */}
      {summarySource && (
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-xs text-grey-400">Action plan</span>
          <span className="text-xs text-grey-300">
            {summarySource === "ai" ? "AI-prioritised" : "Plain summary (AI unavailable)"}
          </span>
        </div>
      )}

      {provenance && (
        <div className="pt-3 border-t border-grey-700/60">
          <ModelLine provenance={provenance} />
        </div>
      )}

      {/* The load-bearing sentence on this panel. Everything above is a real
          measurement; this says what those measurements do not cover, so the
          numbers can't be read as a pass prediction. */}
      {grounding && (
        <p className="pt-1 text-[11px] text-grey-500 leading-relaxed flex gap-1.5">
          <Info size={11} className="shrink-0 mt-0.5 text-grey-600" />
          <span>
            We check that the code parses and that its selectors exist in your repo — we don&apos;t
            run the tests against your app. Run them to confirm they pass.
          </span>
        </p>
      )}
    </div>
  );
}
