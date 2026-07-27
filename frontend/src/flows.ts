import {
  Sparkles, Rocket, ShieldCheck, User, Server, Brain, Github, type LucideIcon,
} from "lucide-react";
import { SERIES_COLOR, type ActivityKind } from "./utils/chartPalette";

/**
 * The three products, described once, as steps.
 *
 * Two screens render this: `/how-it-works` explains the flow to someone
 * deciding whether to hand us a private repo, and `FlowPipeline` lights the
 * same steps up as they actually happen. They were separate lists for about an
 * hour and immediately disagreed — hence one definition with a `driver` field
 * rather than two lists and a convention.
 *
 * `title`/`desc` carry the story for a visitor; `tags` carry the wiring for a
 * developer. Neither audience should have to read past the other.
 *
 * `id` is a contract. For server-driven steps it must equal the id emitted by
 * `backend/services/progress.py` — a mismatch means the card never lights up,
 * which is silent here and loud there (Progress rejects unknown ids).
 */

export type Lane = "you" | "server" | "ai" | "github";

export const LANES: Record<Lane, { label: string; icon: LucideIcon; color: string }> = {
  // Fixed across all three flows, never tinted by the flow's accent. The badge's
  // whole job is "did a model see this?", and tinting it per flow put Generate's
  // accent (a green) beside the server lane (a sage) — two greens meaning
  // opposite things. Flow identity lives in the spine and the node numbers.
  you: { label: "You", icon: User, color: "#9aa3a8" },
  server: { label: "Testra server", icon: Server, color: "#7fb39a" },
  ai: { label: "AI model", icon: Brain, color: "#a99fc9" },
  github: { label: "GitHub", icon: Github, color: "#e0b877" },
};

/** Who reports a step's state while it runs.
 *
 *  `server`  — an NDJSON step event from the endpoint carries it.
 *  `client`  — the browser can see the boundary itself (an SSE opening, a
 *              request returning), so it sets the state locally. Still a real
 *              observation, just a different observer.
 *  `static`  — a step the user performs. It has no runtime state and never
 *              appears in the live pipeline, only on /how-it-works. */
export type Driver = "server" | "client" | "static";

export interface FlowStep {
  id: string;
  title: string;
  desc: string;
  lane: Lane;
  tags: string[];
  driver: Driver;
}

export interface Flow {
  key: ActivityKind;
  label: string;
  icon: LucideIcon;
  headline: string;
  tagline: string;
  to: string;
  cta: string;
  steps: FlowStep[];
}

export const FLOWS: Flow[] = [
  {
    key: "generate",
    label: "Generate",
    icon: Sparkles,
    headline: "From a live site to a running test suite",
    tagline:
      "We render your deployed app, read its real elements, and write tests grounded in what's actually on the page — not guessed from source.",
    to: "/generate",
    cta: "Generate tests",
    steps: [
      {
        id: "input",
        driver: "static",
        lane: "you",
        title: "You point us at your repo and live site",
        desc: "Connect GitHub, then give your repository URL and the deployed, publicly-reachable URL of the app.",
        tags: ["POST /api/analyze-crawl"],
      },
      {
        id: "crawl",
        // `client`, not `server`: /api/analyze-crawl is a plain POST that returns
        // JSON — it never opens an NDJSON stream, and "crawl" is deliberately
        // absent from progress.py's KNOWN_STEPS. Generate.tsx sets this card from
        // the request's own start and finish. Labelling it `server` claimed a
        // step event that nothing emits, which is the exact drift the id contract
        // exists to prevent.
        driver: "client",
        lane: "server",
        title: "We render and crawl your live site",
        desc: "A headless browser runs your app, follows its own links, and indexes the real selectors every page actually renders — ids, data-testid, roles, visible text. Nothing is executed; we only read the DOM. If the site can't be crawled, we stop and say why.",
        tags: ["services/live_crawler.py", "SSRF-guarded"],
      },
      {
        id: "agent2",
        driver: "client",
        lane: "ai",
        title: "The AI writes the suite from the live DOM",
        desc: "The real, rendered selectors are handed to the model — no source-code blob, so far fewer tokens and no guessing. Each file is generated in its own call so a big suite can't truncate, streaming back token by token.",
        tags: ["GET /api/stream/{job_id}", "agents/writer_agent.py", "crawl-only"],
      },
      {
        id: "scaffold",
        driver: "client",
        lane: "server",
        title: "We add the parts that make it runnable",
        desc: "Config, dependency manifest, CI pipelines and a README are templated, not asked of an LLM — one correct answer per framework. Every selector the model wrote is verified back against the live crawl, and anything that misses is healed. Your hosted URL is baked into a single runnable suite.",
        tags: ["POST /api/generate/{job_id}", "agents/scaffold.py", "e2e/"],
      },
      {
        id: "download",
        driver: "static",
        lane: "you",
        title: "You download it and run it",
        desc: "Zipped in your browser — the files never make a second trip. Your live URL is already baked in, so the suite runs against it out of the box.",
        tags: ["utils/download.ts", "JSZip"],
      },
    ],
  },
  {
    key: "publish",
    label: "Publish",
    icon: Rocket,
    headline: "From a ZIP on your desktop to a green repo on GitHub",
    tagline:
      "The tests are written and validated before we touch your account — so what lands in git is already passing, and we push exactly once.",
    to: "/publish",
    cta: "Publish a project",
    steps: [
      {
        id: "connect",
        driver: "static",
        lane: "you",
        title: "You connect GitHub and name the repo",
        desc: "One OAuth round-trip and we can push on your behalf. Pick the name, private or public, and whether to add an E2E suite and CI pipeline on the way in.",
        tags: ["GET /api/auth/github/login", "POST /api/publish-zip"],
      },
      {
        id: "read",
        driver: "server",
        lane: "server",
        title: "We open your archive",
        desc: "Unzipped in memory, never written to disk.",
        tags: ["extract_zip()"],
      },
      {
        id: "filter",
        driver: "server",
        lane: "server",
        title: "We strip what doesn't belong in git",
        desc: "node_modules, build output, binaries and anything oversized come out before the push, and you're told what was dropped. If there's no .gitignore, you get one.",
        tags: ["filter_for_push()"],
      },
      {
        id: "suite",
        driver: "server",
        lane: "ai",
        title: "The suite is written and fixed until it's green",
        desc: "Same two agents as Generate, but with the self-heal loop turned up: any file that fails validation goes back to the model, up to four times. All of it before the repo exists — a red pipeline on your first commit is worse than no pipeline. If the AI is down, your code still ships without it.",
        tags: ["FilterAgent → WriterAgent", "self_heal=True", "max_heal_attempts=4"],
      },
      {
        id: "push",
        driver: "server",
        lane: "github",
        title: "We create the repo and push it as one commit",
        desc: "Six calls to GitHub's Git Data API build the commit properly: resolve you, create the repo, upload each file as a blob, assemble a tree, write the commit, move the branch. One commit, not a file-by-file trickle.",
        tags: ["services/git_publisher.py", "blobs → tree → commit → ref"],
      },
      {
        id: "record",
        driver: "server",
        lane: "server",
        title: "We remember what we made you",
        desc: "The repo is recorded against your account — that's what puts it on your dashboard, and it's why the one-click undo can only ever delete a repo Testra created for you.",
        tags: ["store.record_published_repo()", "DELETE /api/repo/{owner}/{repo}"],
      },
    ],
  },
  {
    key: "scan",
    label: "Scan",
    icon: ShieldCheck,
    headline: "From a live URL to a prioritized fix list",
    tagline:
      "Passive by design. We read what your server volunteers to any visitor — no payloads, no probing, nothing that could knock a site over.",
    to: "/scan",
    cta: "Scan a site",
    steps: [
      {
        id: "input",
        driver: "static",
        lane: "you",
        title: "You give us a deployed URL",
        desc: "Wherever it's live — staging or production. No agent to install, no access to grant.",
        tags: ["POST /api/scan"],
      },
      {
        id: "target",
        driver: "server",
        lane: "server",
        title: "We check the target is fair game",
        desc: "The URL has to resolve to a public host. Private ranges, localhost and cloud metadata addresses are refused, and every redirect hop is re-checked — so this endpoint can't be pointed back at our own network.",
        tags: ["_validate_target()", "_assert_public_host()", "_redirect_guard()"],
      },
      {
        id: "checks",
        driver: "server",
        lane: "server",
        title: "One page load, seven checks",
        desc: "We fetch your page once as an ordinary visitor, then read the answer seven ways: TLS and HTTP→HTTPS, the security headers, cookie flags, version disclosure, CORS, secrets in the page body, and a short list of files nobody meant to deploy.",
        tags: ["services/security_scanner.py", "_check_transport", "_check_cookies", "_check_exposed_files"],
      },
      {
        id: "score",
        driver: "server",
        lane: "server",
        title: "Findings become a score and a grade",
        desc: "Each finding is weighted by severity and comes with the fix — the actual header, flag or redirect to add, plus a video when it's easier watched than read.",
        tags: ["_build_result()", "score/100", "grade A–F"],
      },
      {
        id: "plan",
        driver: "server",
        lane: "ai",
        title: "AI turns the list into a plan",
        desc: "A long list of findings doesn't tell you where to start. One model call ranks it: the risk level, the two or three things to fix first and why, and what's already fine. If the model is unavailable you still get the findings and a plain summary.",
        tags: ["_ai_scan_summary()", "_fallback_summary()"],
      },
      {
        id: "save",
        driver: "server",
        lane: "server",
        title: "The scan is kept in your history",
        desc: "Grade, score and counts land on your dashboard so you can watch it improve. If that write fails you still get your result — bookkeeping never sinks the thing you waited for.",
        tags: ["store.record_scan()", "GET /api/dashboard"],
      },
    ],
  },
];

export const flowFor = (key: ActivityKind): Flow => FLOWS.find((f) => f.key === key)!;

/** The steps that appear in the live pipeline — everything the user doesn't do
 *  by hand. */
export const liveSteps = (key: ActivityKind): FlowStep[] =>
  flowFor(key).steps.filter((s) => s.driver !== "static");

export const accentFor = (key: ActivityKind): string => SERIES_COLOR[key];
