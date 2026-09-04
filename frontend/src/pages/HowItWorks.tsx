import { useState } from "react";
import { Link } from "react-router-dom";
import { motion, AnimatePresence, type Variants } from "framer-motion";
import { Workflow, ArrowRight, Brain, type LucideIcon } from "lucide-react";
import Page from "../components/Page";
import PageHeader from "../components/PageHeader";
import { SERIES_COLOR, type ActivityKind } from "../utils/chartPalette";
import { FLOWS, LANES, type Flow, type FlowStep, type Lane } from "../flows";

/**
 * The three products, explained as flows — the static twin of FlowPipeline.
 *
 * The steps come from ../flows so this page and the live pipeline cannot drift;
 * see that file for why the content is shaped the way it is. This page shows
 * every step including the ones the user performs, which the live pipeline
 * necessarily omits.
 */

const EASE = [0.22, 1, 0.36, 1] as [number, number, number, number];

const stepIn: Variants = {
  hidden: { opacity: 0, y: 16 },
  show: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: { delay: Math.min(i, 6) * 0.07, duration: 0.5, ease: EASE },
  }),
};

/** One numbered node + card. The connector line is drawn by the parent. */
function StepRow({ step, index, accent }: { step: FlowStep; index: number; accent: string }) {
  const lane = LANES[step.lane];
  const LaneIcon: LucideIcon = lane.icon;
  // Lane colors are fixed across all three flows, never tinted by the accent.
  // The badge's whole job is "did a model see this?", and tinting it per flow
  // put Generate's accent (a green) next to the server lane (a sage) — two
  // greens carrying opposite meanings. The flow's identity is the spine and the
  // node numbers; the badges stay a legend you learn once.
  const dot = lane.color;

  return (
    <motion.li
      variants={stepIn}
      custom={index}
      initial="hidden"
      whileInView="show"
      viewport={{ once: true, amount: 0.35 }}
      className="relative pl-14 sm:pl-20 pb-5 last:pb-0"
    >
      {/* Node — the flow's accent, so the spine reads as one continuous run. */}
      <div
        className="absolute left-0 top-1 flex h-10 w-10 sm:h-12 sm:w-12 items-center justify-center rounded-2xl font-display text-sm font-bold"
        style={{
          color: accent,
          background: `linear-gradient(160deg, ${accent}26, ${accent}0d)`,
          border: `1px solid ${accent}59`,
          boxShadow: `0 8px 28px -12px ${accent}80`,
        }}
      >
        {index + 1}
      </div>

      <div className="glass rounded-2xl p-5 card-hover">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 mb-1.5">
          <h3 className="font-display text-base font-semibold leading-snug">{step.title}</h3>
          <span
            className="inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide"
            style={{ color: dot, background: `${dot}1a`, border: `1px solid ${dot}3d` }}
          >
            <LaneIcon size={10} /> {lane.label}
          </span>
        </div>

        <p className="text-sm text-grey-300 leading-relaxed">{step.desc}</p>

        <div className="mt-3 flex flex-wrap gap-1.5">
          {step.tags.map((t) => (
            <code
              key={t}
              className="rounded-md border border-grey-700 bg-white/[0.03] px-2 py-1 font-mono text-[11px] text-grey-400"
            >
              {t}
            </code>
          ))}
        </div>
      </div>
    </motion.li>
  );
}

function FlowDiagram({ flow }: { flow: Flow }) {
  const accent = SERIES_COLOR[flow.key];

  return (
    <div>
      <div className="mb-8 text-center">
        <h2 className="font-display text-2xl sm:text-3xl font-bold tracking-tight">{flow.headline}</h2>
        <p className="mt-2.5 mx-auto max-w-2xl text-sm text-grey-300 leading-relaxed">{flow.tagline}</p>
      </div>

      <ol className="relative">
        {/* The spine. Sits behind the nodes, inset so it starts and ends inside
            the first and last node rather than dangling past them. */}
        <div
          className="pointer-events-none absolute top-6 bottom-6 left-5 sm:left-6 w-px -translate-x-1/2"
          style={{ background: `linear-gradient(${accent}00, ${accent}66 12%, ${accent}66 88%, ${accent}00)` }}
          aria-hidden="true"
        />
        {flow.steps.map((s, i) => (
          <StepRow key={s.title} step={s} index={i} accent={accent} />
        ))}
      </ol>

      <div className="mt-8 text-center">
        <Link
          to={flow.to}
          className="inline-flex items-center gap-2 rounded-2xl btn-primary px-6 py-3.5 text-sm font-semibold"
        >
          <flow.icon size={16} /> {flow.cta} <ArrowRight size={16} />
        </Link>
      </div>
    </div>
  );
}

export default function HowItWorks() {
  const [active, setActive] = useState<ActivityKind>("generate");
  const flow = FLOWS.find((f) => f.key === active)!;

  return (
    <Page className="pb-16">
      <PageHeader
        icon={Workflow}
        eyebrow="How it works"
        title="What actually happens when you press go"
        subtitle="Every step of all three flows — in plain English, with the API behind it. No black box."
      />

      {/* Flow switcher */}
      <div className="mb-8 flex flex-wrap justify-center gap-2" role="tablist" aria-label="Testra flows">
        {FLOWS.map((f) => {
          const on = f.key === active;
          const c = SERIES_COLOR[f.key];
          return (
            <button
              key={f.key}
              role="tab"
              aria-selected={on}
              onClick={() => setActive(f.key)}
              className={`relative inline-flex items-center gap-2 rounded-2xl px-4 py-2.5 text-sm font-semibold transition-colors ${
                on ? "" : "glass text-grey-300 hover:text-white"
              }`}
              style={on ? { color: c, background: `${c}1f`, border: `1px solid ${c}59` } : undefined}
            >
              <f.icon size={15} /> {f.label}
              <span className="text-[11px] font-normal text-grey-500">{f.steps.length} steps</span>
            </button>
          );
        })}
      </div>

      {/* Lane legend — what the badge on each step means. */}
      <div className="mb-10 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-xs text-grey-400">
        <span className="text-grey-500">Where each step runs:</span>
        {(Object.keys(LANES) as Lane[]).map((k) => {
          const l = LANES[k];
          return (
            <span key={k} className="inline-flex items-center gap-1.5">
              <l.icon size={12} style={{ color: l.color }} /> {l.label}
            </span>
          );
        })}
      </div>

      <AnimatePresence mode="wait">
        <motion.div
          key={active}
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.35, ease: EASE }}
        >
          <FlowDiagram flow={flow} />
        </motion.div>
      </AnimatePresence>

      {/* The one thing common to all three flows, said once rather than in
          every AI step. */}
      <section className="mt-16">
        <div className="glass rounded-3xl p-6 sm:p-8">
          <div className="flex items-start gap-4">
            <div className="hidden sm:flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-grey-700 bg-white/[0.03]">
              <Brain size={20} className="text-progress-400" />
            </div>
            <div>
              <h3 className="font-display text-lg font-semibold">Every AI step above shares one router</h3>
              <p className="mt-2 text-sm text-grey-300 leading-relaxed">
                We never depend on a single provider. Each call tries Gemini first, then Groq, then
                Claude, rotating through multiple keys per provider and benching any key that gets
                rate-limited until it cools off. That's why a busy afternoon doesn't turn into a
                failed generation — and if every provider really is exhausted, you're told it's our
                outage and your credit is refunded, never charged.
              </p>
              <div className="mt-3 flex flex-wrap gap-1.5">
                {["services/llm_router.py", "Groq → Gemini → Claude", "per-key cooldown on 429", "GET /api/status"].map((t) => (
                  <code key={t} className="rounded-md border border-grey-700 bg-white/[0.03] px-2 py-1 font-mono text-[11px] text-grey-400">
                    {t}
                  </code>
                ))}
              </div>
            </div>
          </div>
        </div>
      </section>
    </Page>
  );
}
