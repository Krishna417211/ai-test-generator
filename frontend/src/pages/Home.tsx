import { lazy, Suspense } from "react";
import { Link } from "react-router-dom";
import { motion, type Variants } from "framer-motion";
import {
  Sparkles, Rocket, ShieldCheck, ArrowRight, Zap, Wand2, GitBranch, Workflow,
} from "lucide-react";
import Page from "../components/Page";
import TiltCard from "../components/TiltCard";

// Code-split the WebGL scene (three.js) so it loads after first paint.
const Hero3D = lazy(() => import("../components/Hero3D"));

const EASE = [0.22, 1, 0.36, 1] as [number, number, number, number];
const fade: Variants = {
  hidden: { opacity: 0, y: 18 },
  show: (i: number) => ({ opacity: 1, y: 0, transition: { delay: 0.08 * i, duration: 0.6, ease: EASE } }),
};

const FEATURES = [
  { to: "/generate", icon: Sparkles, tint: "from-fuchsia-500/25 to-fuchsia-500/5", ring: "group-hover:border-fuchsia-400/50",
    title: "Generate E2E tests", desc: "Point at any repo. A two-agent AI pipeline writes Playwright, Cypress or Selenium suites with real selectors, Page Objects & CI." },
  { to: "/publish", icon: Rocket, tint: "from-iris-500/25 to-iris-500/5", ring: "group-hover:border-iris-400/50",
    title: "Push to GitHub", desc: "Log in with GitHub, drop a ZIP, and we create a fresh repo and push it — with a validated CI/CD pipeline baked in." },
  { to: "/scan", icon: ShieldCheck, tint: "from-cyanx-400/25 to-cyanx-400/5", ring: "group-hover:border-cyanx-300/50",
    title: "Scan for vulnerabilities", desc: "Audit your deployed URL for the misconfigurations that bite in production — with a fix and an AI action plan for each." },
];

const STEPS = [
  { icon: GitBranch, title: "Point at your code", desc: "A GitHub URL or a ZIP — public or private." },
  { icon: Wand2, title: "AI does the work", desc: "Filters your repo, writes tests, validates them, hardens your deploy." },
  { icon: Zap, title: "Ship with confidence", desc: "Download, push to a repo, or get a security report — in seconds." },
];

// Lightweight static syntax highlight for the hero preview (no user input).
export default function Home() {
  return (
    <Page>
      {/* HERO */}
      <section className="relative pt-10 sm:pt-16 pb-8 text-center overflow-hidden">
        {/* WebGL 3D backdrop */}
        <div className="absolute inset-x-0 -top-24 h-[760px] z-0 pointer-events-none sm:pointer-events-auto">
          <Suspense fallback={null}>
            <Hero3D />
          </Suspense>
        </div>
        {/* Legibility scrim between the orb and the text */}
        <div
          className="absolute inset-0 z-[1] pointer-events-none"
          style={{ background: "radial-gradient(74% 62% at 50% 38%, rgba(28,37,41,0.86), rgba(28,37,41,0.32) 64%, transparent 82%)" }}
        />

        <div className="relative z-10">
        <motion.h1 variants={fade} custom={1} initial="hidden" animate="show"
          className="font-display text-5xl sm:text-7xl font-bold tracking-tight leading-[0.95] text-legible">
          Ship <span className="font-script font-normal text-gradient-animated text-6xl sm:text-8xl leading-none pr-1 align-baseline">Fearless.</span>
          <br />
          <span className="text-white/90">Test, push &amp; secure</span>
          <br className="hidden sm:block" /> with one AI.
        </motion.h1>

        <motion.p variants={fade} custom={2} initial="hidden" animate="show"
          className="mt-6 text-lg text-white/85 max-w-2xl mx-auto leading-relaxed text-legible">
          Testra reads your codebase and writes production E2E tests, publishes them to
          GitHub with CI/CD, and scans your live app for vulnerabilities — free, no account.
        </motion.p>

        <motion.div variants={fade} custom={3} initial="hidden" animate="show"
          className="mt-9 flex flex-wrap items-center justify-center gap-3">
          <Link to="/generate" className="inline-flex items-center gap-2 px-6 py-3.5 rounded-2xl btn-primary font-semibold text-sm">
            <Sparkles size={16} /> Generate tests free <ArrowRight size={16} />
          </Link>
          <Link to="/scan" className="inline-flex items-center gap-2 px-6 py-3.5 rounded-2xl btn-ghost font-semibold text-sm text-white/80">
            <ShieldCheck size={16} /> Scan my site
          </Link>
        </motion.div>

        </div>
      </section>

      {/* FEATURES */}
      <section className="pt-16">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
          {FEATURES.map((f, i) => (
            <motion.div key={f.to} variants={fade} custom={i} initial="hidden" whileInView="show" viewport={{ once: true, amount: 0.3 }}>
              <TiltCard className="h-full rounded-2xl">
                <Link to={f.to} className={`group relative block h-full glass rounded-2xl p-6 card-hover ${f.ring}`} style={{ transformStyle: "preserve-3d" }}>
                  <div className={`w-12 h-12 rounded-2xl bg-gradient-to-br ${f.tint} border border-grey-700 flex items-center justify-center mb-4`}
                    style={{ transform: "translateZ(40px)" }}>
                    <f.icon size={22} className="text-white" />
                  </div>
                  <h3 className="font-display text-lg font-semibold mb-2" style={{ transform: "translateZ(28px)" }}>{f.title}</h3>
                  <p className="text-sm text-grey-300 leading-relaxed" style={{ transform: "translateZ(16px)" }}>{f.desc}</p>
                  <div className="mt-4 inline-flex items-center gap-1.5 text-sm font-medium text-brand-300 group-hover:gap-2.5 transition-all">
                    Try it <ArrowRight size={14} />
                  </div>
                </Link>
              </TiltCard>
            </motion.div>
          ))}
        </div>
      </section>

      {/* HOW IT WORKS */}
      <section className="pt-24">
        <div className="text-center mb-12">
          <h2 className="font-display text-3xl sm:text-4xl font-bold tracking-tight">From code to <span className="font-script font-normal text-gradient text-5xl sm:text-6xl leading-none">confidence</span></h2>
          <p className="mt-3 text-grey-300">Three steps. No config. No account.</p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-5 relative">
          {STEPS.map((s, i) => (
            <motion.div key={s.title} variants={fade} custom={i} initial="hidden" whileInView="show" viewport={{ once: true, amount: 0.3 }}
              className="glass rounded-2xl p-6 relative overflow-hidden">
              <div className="absolute -right-3 -top-4 text-[86px] font-display font-bold text-white/[0.04] leading-none">{i + 1}</div>
              <div className="w-11 h-11 rounded-xl bg-brand-gradient/20 border border-grey-700 flex items-center justify-center mb-4 relative">
                <s.icon size={20} className="text-brand-300" />
              </div>
              <h3 className="font-display text-lg font-semibold mb-1.5">{s.title}</h3>
              <p className="text-sm text-grey-300 leading-relaxed">{s.desc}</p>
            </motion.div>
          ))}
        </div>
        {/* The three cards above are the pitch; some visitors want the wiring
            before they hand over a private repo. */}
        <div className="mt-8 text-center">
          <Link to="/how-it-works"
            className="inline-flex items-center gap-1.5 text-sm font-medium text-brand-300 hover:gap-2.5 transition-all">
            <Workflow size={15} /> See every step, and the API behind it <ArrowRight size={14} />
          </Link>
        </div>
      </section>

      {/* FINAL CTA */}
      <section className="pt-24">
        <motion.div variants={fade} custom={0} initial="hidden" whileInView="show" viewport={{ once: true }}
          className="relative overflow-hidden rounded-3xl gradient-border p-10 sm:p-14 text-center">
          <div className="absolute inset-0 bg-brand-radial opacity-70" />
          <div className="relative">
            <h2 className="font-display text-3xl sm:text-4xl font-bold tracking-tight">Your next deploy could be <span className="font-script font-normal text-gradient text-5xl sm:text-6xl leading-none">bulletproof.</span></h2>
            <p className="mt-3 text-white/[72%] max-w-lg mx-auto">Generate a full test suite in the time it takes to read this sentence.</p>
            <div className="mt-7 flex flex-wrap items-center justify-center gap-3">
              <Link to="/generate" className="inline-flex items-center gap-2 px-7 py-3.5 rounded-2xl btn-primary font-semibold text-sm">
                <Sparkles size={16} /> Start now — it's free
              </Link>
            </div>
          </div>
        </motion.div>
      </section>
    </Page>
  );
}
