import { useState } from "react";
import { Download, FileCode, AlertTriangle, CheckCircle, Copy, Check, FolderDown } from "lucide-react";
import type { GenerateResponse, GeneratedFile } from "../types";
import { downloadAsZip, downloadSingleFile } from "../utils/download";
import { frameworkLabel, pluralize } from "../utils/format";
import CodeViewer from "./CodeViewer";
import TrustPanel from "./TrustPanel";

interface Props {
  result: GenerateResponse;
  onReset: () => void;
}

type Kind = "spec" | "pom" | "ci" | "docs" | "config";

/** Classify by basename and directory, never by a substring of the whole path:
 *  every generated file lives under `tests/`, so matching "test" anywhere in
 *  the path tagged the entire suite — CI workflows included — as a test spec. */
function classify(filename: string): Kind {
  const base = filename.split("/").pop() ?? filename;
  const dir = filename.slice(0, filename.length - base.length);

  if (/\.ya?ml$/i.test(base)) return "ci";
  if (/\.mdx?$/i.test(base)) return "docs";
  if (/\.(spec|test)\./i.test(base)) return "spec";
  if (/(^|\/)pages\//i.test(dir) || /Page\.[jt]sx?$/.test(base)) return "pom";
  if (/\.config\./i.test(base)) return "config";
  return "config";
}

const KIND_LABEL: Record<Kind, string> = {
  spec: "Test Spec",
  pom: "Page Object",
  ci: "CI/CD",
  docs: "Docs",
  config: "Config",
};

// Sage marks the specs — the thing the user actually came for. Everything else
// is supporting material and stays neutral, so the eye lands on the tests.
const KIND_BADGE: Record<Kind, string> = {
  spec: "text-brand-300 bg-brand-400/10 border-brand-400/25",
  pom: "text-grey-300 bg-white/[0.06] border-grey-600",
  ci: "text-grey-300 bg-white/[0.06] border-grey-600",
  docs: "text-grey-400 bg-white/[0.04] border-grey-700",
  config: "text-grey-400 bg-white/[0.04] border-grey-700",
};

function FileCard({ file }: { file: GeneratedFile }) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(file.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const kind = classify(file.filename);

  return (
    <div className="rounded-xl border border-grey-700 bg-white/[0.02] overflow-hidden transition-colors hover:border-grey-600">
      <div className="flex items-center justify-between px-4 py-3 bg-white/[0.04]">
        <div className="flex items-center gap-3 min-w-0">
          <FileCode size={14} className="text-grey-500 shrink-0" />
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono text-grey-200 truncate">{file.filename}</span>
              <span className={`shrink-0 text-xs px-1.5 py-0.5 rounded border ${KIND_BADGE[kind]}`}>
                {KIND_LABEL[kind]}
              </span>
            </div>
            <p className="text-xs text-grey-500 mt-0.5 truncate">{file.description}</p>
          </div>
        </div>
        <div className="flex items-center gap-2 ml-3 shrink-0">
          <button
            onClick={copy}
            className="p-1.5 rounded-lg hover:bg-white/10 text-grey-500 hover:text-grey-300 transition-all"
            title="Copy to clipboard"
          >
            {copied ? <Check size={13} className="text-emerald-400" /> : <Copy size={13} />}
          </button>
          <button
            onClick={() => downloadSingleFile(file.filename, file.content)}
            className="p-1.5 rounded-lg hover:bg-white/10 text-grey-500 hover:text-grey-300 transition-all"
            title="Download file"
          >
            <Download size={13} />
          </button>
          <button
            onClick={() => setExpanded(!expanded)}
            className="px-2.5 py-1 rounded-lg text-xs bg-white/10 hover:bg-white/15 text-grey-400 hover:text-white/90 transition-all"
          >
            {expanded ? "Hide" : "Preview"}
          </button>
        </div>
      </div>

      {expanded && (
        <div className="border-t border-grey-700">
          <CodeViewer filename={file.filename} content={file.content} />
        </div>
      )}
    </div>
  );
}

export default function ResultsStep({ result, onReset }: Props) {
  const [downloading, setDownloading] = useState(false);

  const handleDownload = async () => {
    setDownloading(true);
    await downloadAsZip(result.files);
    setDownloading(false);
  };

  // Validation summary (per-file syntax checks from the backend).
  //
  // Counts only files a parser actually read. The server marks config and docs
  // `checked: false` — they pass by default — so including them turned "6/6
  // passed" into a claim about a README and two YAMLs nobody parsed. Older
  // responses have no `checked` field, hence the `!== false` default.
  const validation = (result.validation ?? []).filter(v => v.checked !== false);
  const validCount = validation.filter(v => v.ok).length;
  const allValid = validation.length > 0 && validCount === validation.length;
  const someInvalid = validation.length > 0 && !allValid;

  // One classification drives both the badge and the grouping, so a file can
  // only ever land in a single bucket. Previously the two filters overlapped
  // and every page object was rendered twice.
  const byKind = (kinds: Kind[]) => result.files.filter(f => kinds.includes(classify(f.filename)));

  const groups = [
    { label: "Test Specs", files: byKind(["spec"]) },
    { label: "Page Objects", files: byKind(["pom"]) },
    { label: "Config & CI/CD", files: byKind(["ci", "config", "docs"]) },
  ].filter(g => g.files.length > 0);

  return (
    <div className="w-full max-w-3xl mx-auto space-y-6">
      {/* Summary bar */}
      <div className="rounded-xl border border-grey-700 bg-white/[0.04] p-4 flex items-center justify-between gap-4">
        <div className="flex items-start gap-3 min-w-0">
          {someInvalid
            ? <AlertTriangle size={18} className="text-amber-400 shrink-0 mt-0.5" />
            : <CheckCircle size={18} className="text-brand-400 shrink-0 mt-0.5" />}
          <div className="min-w-0">
            <p className="text-sm font-semibold text-white">
              {pluralize(result.test_count, "test")} across {pluralize(result.files.length, "file")}
            </p>
            <p className="text-xs mt-1">
              <span className="text-grey-500">{frameworkLabel(result.framework)}</span>
              {validation.length > 0 && (
                <span className={someInvalid ? "text-amber-300" : "text-grey-500"}>
                  {" · "}
                  {allValid ? "syntax check passed" : `${validCount}/${validation.length} passed syntax check`}
                </span>
              )}
            </p>
          </div>
        </div>
        <button
          onClick={handleDownload}
          disabled={downloading}
          className="flex items-center gap-2 px-4 py-2 rounded-lg btn-primary text-sm font-semibold disabled:opacity-60 shrink-0"
        >
          <FolderDown size={15} />
          {downloading ? "Zipping..." : "Download All"}
        </button>
      </div>

      {/* What we verified, and which model wrote it. Carries the "we didn't run
          these" disclaimer that used to sit in the summary bar above. */}
      <TrustPanel grounding={result.grounding} provenance={result.provenance} />

      {/* Selector warnings */}
      {result.selector_warnings.length > 0 && (
        <div className="rounded-xl border border-amber-500/20 bg-amber-500/5 p-4">
          <div className="flex items-center gap-2 mb-2">
            <AlertTriangle size={14} className="text-amber-400" />
            <span className="text-sm font-semibold text-amber-300">Selector Warnings</span>
          </div>
          <ul className="space-y-1">
            {result.selector_warnings.map((w, i) => (
              <li key={i} className="text-xs text-amber-200/60 flex gap-2">
                <span className="shrink-0">•</span>
                <span className="font-mono">{w}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* File groups */}
      {groups.map(({ label, files }) => (
        <div key={label}>
          <div className="text-xs font-semibold uppercase tracking-wider mb-3 text-grey-400">
            {label} ({files.length})
          </div>
          <div className="space-y-2">
            {files.map((file) => (
              <FileCard key={file.filename} file={file} />
            ))}
          </div>
        </div>
      ))}

      {/* Actions — the sage download lives in the summary bar above; this row
          is the secondary repeat, so both buttons stay neutral. */}
      <div className="flex gap-3 pt-2">
        <button
          onClick={onReset}
          className="flex-1 py-3 rounded-xl border border-grey-700 bg-white/5 hover:bg-white/10 text-grey-300 hover:text-white text-sm font-medium transition-all"
        >
          Generate for another repo
        </button>
        <button
          onClick={handleDownload}
          disabled={downloading}
          className="flex-1 py-3 rounded-xl border border-grey-700 bg-white/5 hover:bg-white/10 text-grey-300 hover:text-white text-sm font-medium transition-all disabled:opacity-60 flex items-center justify-center gap-2"
        >
          <Download size={15} />
          {downloading ? "Zipping..." : "Download ZIP"}
        </button>
      </div>
    </div>
  );
}
