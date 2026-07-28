import { ShieldCheck } from "lucide-react";
import type { ExcludedSecret } from "../types";

/**
 * "We found credentials and did not send them anywhere."
 *
 * Framed as a completed protective action, not a warning, because that is what
 * it is — nothing leaked. But it has to be shown rather than logged: a user
 * whose `.env` silently vanished from a push will deploy the repo, hit a
 * missing-configuration error, and spend an afternoon on a problem we caused.
 * Each file is named for the same reason; a count leaves them guessing which.
 */

export default function ExcludedSecrets({
  files,
  where,
  className = "",
}: {
  files?: ExcludedSecret[] | null;
  /** "sent to the AI" | "pushed to the repo" — the sentence differs by flow. */
  where: string;
  className?: string;
}) {
  if (!files || files.length === 0) return null;

  return (
    <div className={`rounded-xl border border-grey-700 bg-white/[0.02] p-4 ${className}`}>
      <div className="flex items-center gap-2 mb-2">
        <ShieldCheck size={14} className="text-brand-300" />
        <span className="text-sm font-semibold text-grey-200">
          {files.length === 1
            ? "1 sensitive file was kept out"
            : `${files.length} sensitive files were kept out`}
        </span>
      </div>
      <p className="text-xs text-grey-500 mb-3 leading-relaxed">
        These look like credentials, so they were never {where}. Nothing else was
        changed — if your project needs one of them at runtime, add it where you
        keep your secrets.
      </p>
      <ul className="space-y-1.5">
        {files.map((f) => (
          <li key={f.path} className="flex items-baseline justify-between gap-3">
            <code className="text-xs font-mono text-grey-300 truncate">{f.path}</code>
            <span className="text-[11px] text-grey-500 shrink-0 text-right max-w-[55%]">
              {f.reason}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
