import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Terminal, Check, X, Loader2, AlertCircle, ShieldAlert } from "lucide-react";
import Page from "../components/Page";
import PageHeader from "../components/PageHeader";
import { useAuth } from "../context/AuthContext";
import { decideCliLogin } from "../utils/api";

/**
 * The browser half of the CLI's device-code login (RFC 8628).
 *
 * The terminal shows a short code; this page is where it gets approved. The
 * signed-in session is the entire authorisation — the code is designed to be read
 * off one screen and typed into another, so it is not a secret and cannot
 * authorise anything on its own.
 *
 * Which makes this page a consent screen, and it is written as one. It must be
 * possible to arrive here having *not* started a login — someone else's typo, or
 * a genuine attempt to get a code approved — and the right outcome then is
 * "Reject". So:
 *
 *   • nothing happens without an explicit click; a prefilled code is never
 *     auto-approved, however convenient that would be;
 *   • Reject is a real, equally prominent button, not a cancel link;
 *   • the page says plainly what approving grants, before it is granted.
 */

const CODE_LEN = 8;

/** Normalise to XXXX-XXXX. Accepts what people actually type — lower case,
 *  spaces, a missing dash — because the code is a secret shared between two
 *  screens, not a syntax exercise. The server normalises identically. */
function formatCode(raw: string): string {
  const cleaned = raw.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, CODE_LEN);
  return cleaned.length > 4 ? `${cleaned.slice(0, 4)}-${cleaned.slice(4)}` : cleaned;
}

type Outcome = { kind: "approved" | "denied"; message: string };

export default function CliAuth() {
  const { user } = useAuth();
  const [params] = useSearchParams();
  const [code, setCode] = useState(() => formatCode(params.get("code") || ""));
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);
  const [error, setError] = useState("");
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  // Clear a stale error as soon as the code changes — leaving "that code isn't
  // valid" under a freshly corrected code reads as though it is still wrong.
  useEffect(() => setError(""), [code]);

  const decide = useCallback(
    async (decision: "approve" | "deny") => {
      const complete = code.replace("-", "");
      if (complete.length !== CODE_LEN) {
        setError("Enter the full 8-character code shown in your terminal.");
        return;
      }
      setBusy(decision);
      setError("");
      try {
        const message = await decideCliLogin(code, decision);
        setOutcome({ kind: decision === "approve" ? "approved" : "denied", message });
      } catch (err: any) {
        setError(err.message);
      } finally {
        setBusy(null);
      }
    },
    [code],
  );

  if (outcome) {
    const approved = outcome.kind === "approved";
    return (
      <Page>
        <div className="max-w-md mx-auto pt-10">
          <div
            className={`rounded-2xl border p-6 text-center ${
              approved
                ? "border-brand-400/40 bg-brand-400/[0.06]"
                : "border-grey-700 bg-white/[0.03]"
            }`}
          >
            <div
              className={`w-11 h-11 rounded-full mx-auto mb-4 flex items-center justify-center ${
                approved ? "bg-brand-400/15 text-brand-300" : "bg-white/[0.06] text-grey-400"
              }`}
            >
              {approved ? <Check size={20} /> : <X size={20} />}
            </div>
            <h2 className="text-base font-semibold text-white">
              {approved ? "Your terminal is signed in" : "Request rejected"}
            </h2>
            <p className="text-sm text-grey-400 mt-2 leading-relaxed">
              {approved
                ? "You can close this tab and go back to your terminal."
                : "Nothing was granted. If you didn't start this, no action is needed — but if you keep seeing codes you didn't request, change your password."}
            </p>
            {outcome.message && (
              <p className="text-xs text-grey-500 mt-3">{outcome.message}</p>
            )}
          </div>
        </div>
      </Page>
    );
  }

  return (
    <Page>
      <PageHeader
        icon={Terminal}
        eyebrow="Command line"
        title="Approve a terminal sign-in"
        subtitle="Enter the code shown by the Testra CLI. It expires 10 minutes after it appears."
      />

      <div className="max-w-md mx-auto space-y-5">
        <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-5 sm:p-6 space-y-4">
          <label htmlFor="cli-code" className="block text-xs font-semibold uppercase tracking-wider text-grey-400">
            Code from your terminal
          </label>
          <input
            id="cli-code"
            value={code}
            onChange={(e) => setCode(formatCode(e.target.value))}
            placeholder="ABCD-EFGH"
            autoComplete="off"
            spellCheck={false}
            className="w-full px-4 py-3.5 rounded-xl bg-black/30 border border-grey-700 text-white text-center text-xl font-mono tracking-[0.25em] placeholder:text-grey-600 placeholder:tracking-[0.25em] focus:outline-none focus:border-brand-500/70 transition-colors"
          />

          {/* What approving actually grants, said before it is granted rather
              than in a tooltip afterwards. */}
          <div className="rounded-xl border border-grey-700/70 bg-white/[0.02] p-3.5 space-y-2">
            <div className="flex items-center gap-2">
              <ShieldAlert size={13} className="text-grey-500 shrink-0" />
              <span className="text-xs font-semibold text-grey-300">
                Approving signs that terminal in as
                {user?.email ? <span className="text-white"> {user.email}</span> : " you"}
              </span>
            </div>
            <p className="text-[11px] text-grey-500 leading-relaxed">
              It will be able to publish repositories to your linked GitHub account
              and spend your generation quota, until you sign out everywhere in
              Settings.
            </p>
          </div>

          {error && (
            <div className="flex items-start gap-2 text-xs text-rose-300">
              <AlertCircle size={13} className="mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {/* Reject is a peer of Approve, not a cancel link. Someone may well be
              here because a code they never asked for turned up. */}
          <div className="flex gap-3 pt-1">
            <button
              onClick={() => decide("deny")}
              disabled={busy !== null}
              className="flex-1 py-3 rounded-xl border border-grey-700 bg-white/5 hover:bg-white/10 text-grey-300 hover:text-white text-sm font-medium transition-all disabled:opacity-60 flex items-center justify-center gap-2"
            >
              {busy === "deny" ? <Loader2 size={15} className="animate-spin" /> : <X size={15} />}
              Reject
            </button>
            <button
              onClick={() => decide("approve")}
              disabled={busy !== null}
              className="flex-1 py-3 rounded-xl btn-primary text-sm font-semibold disabled:opacity-60 flex items-center justify-center gap-2"
            >
              {busy === "approve" ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />}
              Approve
            </button>
          </div>
        </div>

        <p className="text-xs text-grey-500 text-center leading-relaxed">
          Didn't start this? Choose <span className="text-grey-300">Reject</span>. A
          code on its own grants nothing — it only works once someone signed in
          approves it here.
        </p>
      </div>
    </Page>
  );
}
