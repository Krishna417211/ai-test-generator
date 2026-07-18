import { motion, AnimatePresence } from "framer-motion";
import { Check, Loader2, Minus, type LucideIcon } from "lucide-react";
import type { ActivityKind } from "../utils/chartPalette";
import { LANES, accentFor, liveSteps, type FlowStep } from "../flows";
import type { StepEvent, StepState } from "../utils/api";

/**
 * The pipeline, lit up as it actually runs.
 *
 * Every state here is reported — by the server over NDJSON, or by the browser
 * observing a boundary it can genuinely see (see `driver` in flows.ts). Nothing
 * advances on a timer. That constraint is the feature: a step that says "done"
 * is done, and one that sits spinning for 20 seconds is telling the truth about
 * where the time went, which is the thing a spinner could never do.
 *
 * `/how-it-works` renders the same steps from the same definition, minus the
 * live state.
 */

export type StepStates = Record<string, { state: StepState; detail?: string }>;

/** Fold a step event into the state map. Kept here so every caller reduces
 *  events identically — Generate, Scan and Publish all just spread this. */
export function applyStep(prev: StepStates, e: StepEvent): StepStates {
  return { ...prev, [e.id]: { state: e.state, detail: e.detail } };
}

const EASE = [0.22, 1, 0.36, 1] as [number, number, number, number];

function StatusIcon({ state, accent }: { state: StepState | "pending"; accent: string }) {
  if (state === "running") {
    return <Loader2 size={14} className="animate-spin" style={{ color: accent }} />;
  }
  if (state === "done") return <Check size={14} style={{ color: accent }} />;
  if (state === "skipped") return <Minus size={14} className="text-grey-500" />;
  return <span className="block h-1.5 w-1.5 rounded-full bg-grey-600" />;
}

function Row({
  step, state, detail, accent, index, isLast,
}: {
  step: FlowStep;
  state: StepState | "pending";
  detail?: string;
  accent: string;
  index: number;
  isLast: boolean;
}) {
  const lane = LANES[step.lane];
  const LaneIcon: LucideIcon = lane.icon;
  const active = state === "running";
  const reached = state !== "pending";

  return (
    <li className="relative flex gap-4">
      {/* Rail: node + the segment joining it to the next node. */}
      <div className="flex flex-col items-center">
        <motion.div
          className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-xl"
          style={{
            background: reached ? `${accent}1f` : "rgba(255,255,255,0.03)",
            border: `1px solid ${reached ? `${accent}59` : "rgba(255,255,255,0.08)"}`,
          }}
          animate={active ? { boxShadow: [`0 0 0 0 ${accent}55`, `0 0 0 8px ${accent}00`] } : {}}
          transition={active ? { duration: 1.8, repeat: Infinity, ease: "easeOut" } : {}}
        >
          <StatusIcon state={state} accent={accent} />
        </motion.div>
        {!isLast && (
          <div className="relative w-px flex-1 my-1 bg-white/[0.07]">
            <motion.div
              className="absolute inset-x-0 top-0 origin-top"
              style={{ background: accent, bottom: 0 }}
              initial={{ scaleY: 0 }}
              animate={{ scaleY: state === "done" || state === "skipped" ? 1 : 0 }}
              transition={{ duration: 0.5, ease: EASE }}
            />
          </div>
        )}
      </div>

      {/* Copy */}
      <div className={`flex-1 min-w-0 pb-5 ${isLast ? "pb-0" : ""}`}>
        <motion.div
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: index * 0.05, duration: 0.4, ease: EASE }}
        >
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
            <h4
              className={`font-display text-sm font-semibold leading-snug transition-colors ${
                reached ? "text-white/90" : "text-grey-500"
              }`}
            >
              {step.title}
            </h4>
            <span
              className="inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide"
              style={{
                color: reached ? lane.color : "#545c60",
                background: reached ? `${lane.color}1a` : "transparent",
                border: `1px solid ${reached ? `${lane.color}3d` : "rgba(255,255,255,0.06)"}`,
              }}
            >
              <LaneIcon size={9} /> {lane.label}
            </span>
          </div>

          {/* The description is the "why" and only earns its space while the step
              is the one you're waiting on. Once it's done the detail — a real
              number from this run — says more in a fraction of the room. */}
          <AnimatePresence initial={false} mode="wait">
            {active && (
              <motion.p
                key="desc"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.3, ease: EASE }}
                className="overflow-hidden text-xs text-grey-400 leading-relaxed"
              >
                <span className="block pt-1.5">{step.desc}</span>
              </motion.p>
            )}
          </AnimatePresence>

          {detail && (
            <motion.div
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3, ease: EASE }}
              className={`mt-1 text-xs ${state === "skipped" ? "text-grey-500 italic" : "text-grey-300"}`}
            >
              {detail}
            </motion.div>
          )}

          {active && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {step.tags.map((t) => (
                <code
                  key={t}
                  className="rounded border border-grey-700 bg-white/[0.03] px-1.5 py-0.5 font-mono text-[10px] text-grey-400"
                >
                  {t}
                </code>
              ))}
            </div>
          )}
        </motion.div>
      </div>
    </li>
  );
}

export default function FlowPipeline({
  flow, states, title,
}: {
  flow: ActivityKind;
  states: StepStates;
  title?: string;
}) {
  const steps = liveSteps(flow);
  const accent = accentFor(flow);
  const doneCount = steps.filter(
    (s) => states[s.id]?.state === "done" || states[s.id]?.state === "skipped",
  ).length;

  return (
    <div className="glass rounded-2xl p-5">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div className="text-xs font-semibold uppercase tracking-wider text-grey-400">
          {title ?? "What's happening"}
        </div>
        <div className="font-mono text-[11px] text-grey-500">
          {doneCount}/{steps.length}
        </div>
      </div>

      <ol>
        {steps.map((s, i) => (
          <Row
            key={s.id}
            step={s}
            state={states[s.id]?.state ?? "pending"}
            detail={states[s.id]?.detail}
            accent={accent}
            index={i}
            isLast={i === steps.length - 1}
          />
        ))}
      </ol>
    </div>
  );
}
