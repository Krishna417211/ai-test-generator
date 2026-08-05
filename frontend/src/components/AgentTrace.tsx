import {
  Bot, Search, FileText, Crosshair, CheckCircle2, PlayCircle,
  PenLine, AlertTriangle, XCircle,
} from "lucide-react";
import type { AgentTrace as Trace } from "../types";

/**
 * What the agent actually did — not a claim that it did anything.
 *
 * This panel renders only when the agentic path produced the suite. A run that
 * asked for agent mode and fell back to the scripted pipeline sends no trace,
 * and therefore shows nothing here rather than an empty agent panel implying
 * an agent ran. That distinction is the whole reason the backend never
 * synthesises this object.
 *
 * The test-run row is the one genuinely new claim in the product: everywhere
 * else we are careful to say we did NOT run the tests (see TrustPanel). When
 * `last_run` is present we did, so it may say so — and when it is absent the
 * row is omitted entirely, because "not run" must never be shown in a way that
 * reads as "nothing failed".
 */

interface Props {
  agent?: Trace | null;
  className?: string;
}

const TOOL_ICON: Record<string, typeof Search> = {
  search_source: Search,
  read_file: FileText,
  list_files: FileText,
  find_anchors: Crosshair,
  validate_code: CheckCircle2,
  run_test: PlayCircle,
  write_file: PenLine,
};

const TOOL_LABEL: Record<string, string> = {
  search_source: "Searched the source",
  read_file: "Read a file",
  list_files: "Listed the files",
  find_anchors: "Checked real selectors",
  validate_code: "Validated a file",
  run_test: "Ran the suite",
  write_file: "Wrote a file",
};

/** Why the agent stopped, in words that say whether the suite is complete. */
const STOP_NOTE: Record<string, { text: string; warn: boolean }> = {
  completed: { text: "Finished on its own", warn: false },
  max_turns: {
    text: "Hit the step limit — the suite may be incomplete",
    warn: true,
  },
  exhausted: {
    text: "Ran out of AI capacity partway — the suite may be incomplete",
    warn: true,
  },
};

export default function AgentTrace({ agent, className = "" }: Props) {
  if (!agent) return null;

  const stop = STOP_NOTE[agent.stop_reason] ?? {
    text: agent.stop_reason,
    warn: true,
  };
  const run = agent.last_run;

  return (
    <div
      className={`rounded-xl border border-white/10 bg-white/[0.02] p-4 ${className}`}
    >
      <div className="flex items-center gap-2 mb-3">
        <Bot size={14} className="text-brand-300" />
        <span className="text-sm font-semibold text-grey-100">
          Agent mode — how this suite was written
        </span>
      </div>

      <p className="text-xs text-grey-400 mb-3">
        The model investigated your app itself over{" "}
        <span className="text-grey-200">{agent.turns}</span>{" "}
        {agent.turns === 1 ? "step" : "steps"}, checking each selector against
        what really exists before writing{" "}
        <span className="text-grey-200">{agent.files_written}</span>{" "}
        {agent.files_written === 1 ? "file" : "files"}
        {!!agent.providers?.length && (
          <> — on {agent.providers.join(" and ")}</>
        )}
        .
      </p>

      {/* The only place in the product allowed to say the tests were run. */}
      {run && !run.timed_out && (
        <div
          className={`rounded-lg border p-3 mb-3 ${
            run.failed === 0 && run.total > 0
              ? "border-brand-400/25 bg-brand-400/5"
              : "border-amber-500/25 bg-amber-500/5"
          }`}
        >
          <div className="flex items-center gap-2">
            <PlayCircle
              size={13}
              className={
                run.failed === 0 && run.total > 0
                  ? "text-brand-300"
                  : "text-amber-300"
              }
            />
            <span className="text-xs font-semibold text-grey-100">
              Actually run against your app — {run.passed}/{run.total} passed
              {run.skipped > 0 && `, ${run.skipped} skipped`}
            </span>
          </div>
          {run.failures.length > 0 && (
            <ul className="mt-2 space-y-1.5">
              {run.failures.slice(0, 5).map((f, i) => (
                <li key={i} className="text-[11px] text-grey-400">
                  <span className="text-amber-300">{f.test}</span>
                  <span className="text-grey-500"> · {f.file}</span>
                  <div className="font-mono text-grey-500 truncate">
                    {f.message.split("\n")[0]}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {run?.timed_out && (
        <div className="rounded-lg border border-amber-500/25 bg-amber-500/5 p-3 mb-3 flex items-center gap-2">
          <AlertTriangle size={13} className="text-amber-300" />
          <span className="text-xs text-grey-300">
            The test run was stopped for taking too long, so these results are
            unverified.
          </span>
        </div>
      )}

      <ol className="space-y-1">
        {agent.tool_calls.map((call, i) => {
          const Icon = TOOL_ICON[call.name] ?? CheckCircle2;
          return (
            <li key={i} className="flex items-start gap-2 text-[11px]">
              {call.ok ? (
                <Icon size={12} className="text-grey-500 mt-0.5 shrink-0" />
              ) : (
                <XCircle size={12} className="text-amber-400 mt-0.5 shrink-0" />
              )}
              <span className="text-grey-400">
                {TOOL_LABEL[call.name] ?? call.name}
                <span className="text-grey-600"> · {call.summary}</span>
              </span>
            </li>
          );
        })}
      </ol>

      <div
        className={`mt-3 pt-2 border-t border-white/5 text-[11px] ${
          stop.warn ? "text-amber-300" : "text-grey-500"
        }`}
      >
        {stop.warn && (
          <AlertTriangle size={11} className="inline mr-1 -mt-0.5" />
        )}
        {stop.text}
      </div>
    </div>
  );
}
