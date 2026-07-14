import { useState } from "react";
import { Download, FileCode, AlertTriangle, CheckCircle, Copy, Check, FolderDown } from "lucide-react";
import type { GenerateResponse, GeneratedFile } from "../types";
import { downloadAsZip, downloadSingleFile } from "../utils/download";
import CodeViewer from "./CodeViewer";

interface Props {
  result: GenerateResponse;
  onReset: () => void;
}

function FileCard({ file }: { file: GeneratedFile }) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(file.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const isTest = file.filename.includes("spec") || file.filename.includes("test");
  const isCI = file.filename.includes(".yml") || file.filename.includes(".yaml");
  const isPOM = file.filename.includes("Page") || file.filename.includes("pages/");

  const tagColor = isTest
    ? "text-emerald-400 bg-emerald-400/10 border-emerald-400/20"
    : isCI
    ? "text-iris-400 bg-iris-400/10 border-iris-400/20"
    : isPOM
    ? "text-progress-400 bg-progress-400/10 border-progress-400/20"
    : "text-grey-500 bg-white/5 border-grey-700";

  const tag = isTest ? "Test Spec" : isCI ? "CI/CD" : isPOM ? "Page Object" : "Config";

  return (
    <div className="rounded-xl border border-grey-700 bg-white/3 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 bg-white/5">
        <div className="flex items-center gap-3 min-w-0">
          <FileCode size={14} className="text-grey-500 shrink-0" />
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono text-white/80 truncate">{file.filename}</span>
              <span className={`shrink-0 text-xs px-1.5 py-0.5 rounded border ${tagColor}`}>
                {tag}
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

  // Validation summary (per-file syntax checks from the backend)
  const validation = result.validation ?? [];
  const validCount = validation.filter(v => v.ok).length;
  const allValid = validation.length > 0 && validCount === validation.length;

  // Group files
  const specs = result.files.filter(f => f.filename.includes("spec") || f.filename.includes("test"));
  const pages = result.files.filter(f => f.filename.includes("pages/") || f.filename.includes("Page"));
  const configs = result.files.filter(f => !specs.includes(f) && !pages.includes(f));

  const groups = [
    { label: "Test Specs", files: specs, color: "text-emerald-400" },
    { label: "Page Objects", files: pages, color: "text-progress-400" },
    { label: "Config & CI/CD", files: configs, color: "text-iris-400" },
  ].filter(g => g.files.length > 0);

  return (
    <div className="w-full max-w-3xl mx-auto space-y-6">
      {/* Summary bar */}
      <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <CheckCircle size={18} className="text-emerald-400" />
          <div>
            <p className="text-sm font-semibold text-white">{result.summary}</p>
            <p className="text-xs text-grey-500 mt-0.5">
              {result.files.length} files · {result.test_count} test cases · {result.framework}
            </p>
            {validation.length > 0 && (
              <span
                className={`inline-flex items-center gap-1 mt-2 text-xs px-2 py-0.5 rounded-full border ${
                  allValid
                    ? "text-emerald-300 bg-emerald-400/10 border-emerald-400/20"
                    : "text-amber-300 bg-amber-400/10 border-amber-400/20"
                }`}
              >
                <CheckCircle size={11} />
                {allValid
                  ? "All files passed a syntax check"
                  : `${validCount}/${validation.length} passed a syntax check`}
              </span>
            )}
            {validation.length > 0 && (
              <p className="mt-1.5 text-[11px] text-grey-500">
                Syntax-checked only — not executed. Run them against your app to confirm they pass.
              </p>
            )}
          </div>
        </div>
        <button
          onClick={handleDownload}
          disabled={downloading}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-semibold transition-all disabled:opacity-60 shrink-0"
        >
          <FolderDown size={15} />
          {downloading ? "Zipping..." : "Download All"}
        </button>
      </div>

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
      {groups.map(({ label, files, color }) => (
        <div key={label}>
          <div className={`text-xs font-semibold uppercase tracking-wider mb-3 ${color}`}>
            {label} ({files.length})
          </div>
          <div className="space-y-2">
            {files.map((file) => (
              <FileCard key={file.filename} file={file} />
            ))}
          </div>
        </div>
      ))}

      {/* Actions */}
      <div className="flex gap-3 pt-2">
        <button
          onClick={onReset}
          className="flex-1 py-3 rounded-xl border border-grey-700 bg-white/5 hover:bg-white/10 text-grey-300 hover:text-white text-sm font-medium transition-all"
        >
          Generate for another repo
        </button>
        <button
          onClick={handleDownload}
          className="flex-1 py-3 rounded-xl bg-progress-600 hover:bg-progress-500 text-white text-sm font-semibold transition-all flex items-center justify-center gap-2"
        >
          <Download size={15} />
          Download ZIP
        </button>
      </div>
    </div>
  );
}
