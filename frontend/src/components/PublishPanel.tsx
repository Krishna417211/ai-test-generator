import { useState, useRef, useEffect } from "react";
import {
  Github, Upload, Lock, Loader2, Rocket, CheckCircle2, ExternalLink, AlertTriangle, Trash2,
} from "lucide-react";
import { Link } from "react-router-dom";
import { publishZip, getAuthConfig, validateZip, deleteRepo, githubLoginUrl } from "../utils/api";
import FlowPipeline, { applyStep, type StepStates } from "./FlowPipeline";
import TrustPanel from "./TrustPanel";
import SuccessRate from "./SuccessRate";
import ExcludedSecrets from "./ExcludedSecrets";
import { pluralize } from "../utils/format";
import type { PublishResult } from "../types";
import { useAuth } from "../context/AuthContext";
import { celebrate } from "../lib/celebrate";

// Connecting GitHub means leaving the page, so whatever has been typed is kept
// here and restored on the way back. A File cannot go in sessionStorage (it is
// a live handle to a file on disk, not data), so `hadFile` records only that
// one was chosen — enough to ask for it again by name instead of silently
// presenting an empty dropzone as though nothing was lost.
const DRAFT_KEY = "testra_publish_draft";

interface PublishDraft {
  repoName: string;
  addCicd: boolean;
  isPrivate: boolean;
  fileName: string | null;
}

function saveDraft(draft: PublishDraft): void {
  try {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  } catch {
    // Private-mode or a full quota — losing the draft is a worse experience,
    // not a broken one. Never block the connect on it.
  }
}

function takeDraft(): PublishDraft | null {
  try {
    const raw = sessionStorage.getItem(DRAFT_KEY);
    sessionStorage.removeItem(DRAFT_KEY); // one-shot: only the trip back restores
    return raw ? (JSON.parse(raw) as PublishDraft) : null;
  } catch {
    return null;
  }
}

export default function PublishPanel() {
  const { user, refresh } = useAuth();
  // What matters here is whether *this session* can push, not whether the
  // account has a GitHub identity on file — see User.github_connected. Only
  // /api/auth/me reports it, and a password login populated `user` from the
  // login response, so re-ask on mount rather than trust what's in hand.
  const hasGithub = Boolean(user?.github_connected);

  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const [repoName, setRepoName] = useState("");
  const [ghToken, setGhToken] = useState("");           // PAT fallback (OAuth disabled)
  const [addCicd, setAddCicd] = useState(true);
  const [isPrivate, setIsPrivate] = useState(true);
  const [publishing, setPublishing] = useState(false);
  const [pubError, setPubError] = useState<string | null>(null);
  const [pubResult, setPubResult] = useState<PublishResult | null>(null);
  // "unknown" until the server answers. It starts optimistic on purpose: the
  // only alternative to the GitHub button is a "paste a Personal Access Token"
  // box, and showing that because a config fetch was slow or failed asks the
  // user for a credential the product is built to never need.
  const [oauth, setOauth] = useState<"unknown" | "on" | "off">("unknown");
  const oauthEnabled = oauth !== "off";
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deletedRepo, setDeletedRepo] = useState<string | null>(null);
  const [steps, setSteps] = useState<StepStates>({});
  const [connecting, setConnecting] = useState(false);
  // The ZIP that was picked before connecting. Set only when a draft comes
  // back, so the dropzone can name what it needs rather than look untouched.
  const [restoredFileName, setRestoredFileName] = useState<string | null>(null);

  useEffect(() => {
    getAuthConfig()
      .then((c) => setOauth(c.github_oauth_enabled ? "on" : "off"))
      .catch(() => {});
    // Refresh the session-derived github_connected flag. Also what makes the
    // return trip from OAuth land on a panel that already knows it's connected.
    refresh().catch(() => {});

    // Put the form back the way it was left before the OAuth round-trip.
    const draft = takeDraft();
    if (draft) {
      setRepoName(draft.repoName);
      setAddCicd(draft.addCicd);
      setIsPrivate(draft.isPrivate);
      setRestoredFileName(draft.fileName);
    }
  }, []);

  const connectGithub = () => {
    setConnecting(true);
    saveDraft({ repoName, addCicd, isPrivate, fileName: file?.name ?? null });
    // Comes back to this page, not Settings — `next` is validated server-side
    // (_safe_next) so only a same-site path is ever honoured.
    window.location.href = githubLoginUrl("/publish");
  };

  // The push button is disabled until all three are satisfied. Track them by
  // name so we can say which one is missing instead of just graying out.
  const missing = [
    !file && "a project ZIP",
    !repoName.trim() && "a repository name",
    !(hasGithub || (!oauthEnabled && ghToken.trim())) &&
      (oauthEnabled ? "a connected GitHub account" : "a GitHub token"),
  ].filter(Boolean) as string[];
  const canPublish = missing.length === 0;

  const selectFile = (f: File | undefined) => {
    if (!f) return;
    const invalid = validateZip(f);
    setPubError(invalid);
    setFile(invalid ? null : f);
    if (!invalid) setRestoredFileName(null); // the ask has been answered
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault(); setDragOver(false);
    selectFile(e.dataTransfer.files[0]);
  };

  const handleDelete = async () => {
    if (!pubResult) return;
    setDeleting(true); setDeleteError(null);
    try {
      await deleteRepo(pubResult.full_name);
      setDeletedRepo(pubResult.full_name);
      setPubResult(null);
      setConfirmingDelete(false);
    } catch (e: any) {
      setDeleteError(e.message || "Could not delete the repository");
    } finally { setDeleting(false); }
  };

  const publish = async () => {
    if (!canPublish || !file) return;
    setPublishing(true); setPubError(null); setPubResult(null);
    setDeletedRepo(null); setConfirmingDelete(false); setDeleteError(null);
    setSteps({});
    try {
      const result = await publishZip(file, {
        githubToken: !hasGithub ? ghToken.trim() : undefined,
        repoName: repoName.trim(), addCicd, private: isPrivate,
      }, (e) => setSteps((s) => applyStep(s, e)));
      setPubResult(result);
      celebrate();
    } catch (e: any) {
      // Publish is the one flow where a failure can land after real work — the
      // pipeline is left up so "pushed 40 files, then failed to record" reads
      // differently from "never got off the ground".
      setPubError(e.message || "Publish failed");
    } finally { setPublishing(false); }
  };

  return (
    <div className="w-full max-w-xl mx-auto glass rounded-3xl p-6 shadow-card space-y-5">
      {/* GitHub connection — established in Settings, only reported here. */}
      {hasGithub ? (
        <div className="flex items-center gap-2 px-4 py-3 rounded-2xl bg-emerald-500/10 border border-emerald-500/25 text-sm">
          <Github size={16} className="text-emerald-300" />
          GitHub connected as <span className="font-semibold text-white">{user?.github_login}</span>
          {/* An access token can be revoked on GitHub's side at any time, and
              nothing tells us until a push fails with "invalid or expired".
              Manage it where it was connected, not in the middle of a form. */}
          <Link to="/settings#github" className="ml-auto text-xs text-grey-400 hover:text-white/80 transition-colors shrink-0">
            Manage
          </Link>
        </div>
      ) : oauthEnabled ? (
        // Connect right here rather than sending the user to Settings mid-task.
        // The round-trip does leave the page, so the form is saved first and
        // restored on the way back (see DRAFT_KEY) — everything except the ZIP,
        // which is a live file handle and cannot be serialized.
        <div className="px-4 py-3.5 rounded-2xl bg-white/[0.03] border border-grey-700 space-y-3">
          <div className="flex items-center gap-2 text-sm text-white">
            <Github size={16} className="text-grey-300" />
            {user?.has_github
              // has_github without github_connected means the identity is on the
              // account but this session holds no push token — signing back in
              // with a password does exactly that. Say so, or it reads as though
              // the connection they already made came undone.
              ? "This sign-in doesn't include GitHub push access"
              : "Connect GitHub to publish"}
          </div>

          <button
            onClick={connectGithub}
            disabled={connecting}
            className="w-full flex items-center justify-center gap-2 py-3 rounded-xl bg-[#24292f] hover:bg-[#32383f] border border-white/15 text-white font-semibold text-sm transition-colors disabled:opacity-60"
          >
            {connecting ? (
              <><Loader2 size={16} className="animate-spin" /> Opening GitHub…</>
            ) : (
              <><Github size={16} /> {user?.has_github ? "Reconnect GitHub" : "Connect with GitHub"}</>
            )}
          </button>

          <p className="text-xs text-grey-400">
            Nothing to paste — you approve it on GitHub and land back here.
            Connect once and every push afterwards just works.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex items-start gap-1.5 text-xs text-amber-300/80">
            <AlertTriangle size={12} className="mt-0.5 shrink-0" />
            GitHub login isn't configured — paste a Personal Access Token (repo scope):
          </div>
          <div className="relative">
            <Lock size={14} className="absolute left-4 top-1/2 -translate-y-1/2 text-grey-500" />
            <input type="password" value={ghToken} onChange={(e) => setGhToken(e.target.value)} placeholder="ghp_xxxxxxxxxxxx"
              className="w-full pl-10 pr-4 py-3 rounded-xl bg-black/30 border border-grey-700 text-white placeholder:text-grey-400 focus:outline-none focus:border-brand-500/70 transition-colors text-sm font-mono" />
          </div>
        </div>
      )}

      {/* Dropzone */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        onClick={() => fileRef.current?.click()}
        className={`w-full h-36 rounded-2xl border-2 border-dashed flex flex-col items-center justify-center gap-2.5 cursor-pointer transition-all ${
          dragOver ? "border-brand-400 bg-brand-500/10" : file ? "border-emerald-500/50 bg-emerald-500/10" : "border-white/15 bg-black/20 hover:border-white/30"
        }`}
      >
        <Upload size={20} className={file ? "text-emerald-400" : "text-grey-500"} />
        <p className="text-sm text-grey-300">{file ? file.name : "Drop your project ZIP here"}</p>
        {/* A browser will not hand a file back across a page load, so after
            connecting we ask for the same one by name instead of pretending it
            was never chosen. */}
        {!file && restoredFileName && (
          <p className="text-xs text-amber-300/80 px-4 text-center">
            Pick <span className="font-mono">{restoredFileName}</span> again — browsers can't
            carry a file across the GitHub sign-in.
          </p>
        )}
        <input ref={fileRef} type="file" accept=".zip" className="hidden" onChange={(e) => selectFile(e.target.files?.[0])} />
      </div>

      <input type="text" value={repoName} onChange={(e) => setRepoName(e.target.value)} placeholder="new-repo-name"
        className="w-full px-4 py-3 rounded-xl bg-black/30 border border-grey-700 text-white placeholder:text-grey-400 focus:outline-none focus:border-brand-500/70 transition-colors text-sm font-mono" />

      <div className="flex flex-col gap-2.5">
        <label className="flex items-center gap-2.5 text-sm text-grey-300 cursor-pointer">
          <input type="checkbox" checked={addCicd} onChange={(e) => setAddCicd(e.target.checked)} className="w-4 h-4 accent-brand-500" />
          Add CI/CD pipeline (generate + validate E2E tests, then push)
        </label>
        <label className="flex items-center gap-2.5 text-sm text-grey-300 cursor-pointer">
          <input type="checkbox" checked={isPrivate} onChange={(e) => setIsPrivate(e.target.checked)} className="w-4 h-4 accent-brand-500" />
          Create as a private repository
        </label>
      </div>

      {pubError && <div className="px-4 py-3 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300">{pubError}</div>}

      {pubResult && (
        <div className="px-4 py-4 rounded-2xl bg-emerald-500/10 border border-emerald-500/30 space-y-2.5">
          <div className="flex items-center gap-2 text-emerald-300 font-semibold text-sm">
            <CheckCircle2 size={16} /> Pushed to GitHub 🎉
          </div>
          <a href={pubResult.repo_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 text-sm text-brand-300 hover:text-brand-200 font-mono break-all">
            {pubResult.full_name} <ExternalLink size={13} />
          </a>
          <div className="text-xs text-grey-300 space-y-1">
            <div>
              {pluralize(pubResult.files_pushed, "file")} pushed to <span className="font-mono">{pubResult.branch}</span>
              {/* "N files pushed" is a claim about the repo, so say whether we
                  went and checked. Every write in a push can return 2xx and
                  still leave the branch short a file; the server reads the
                  pushed tree back and compares content hashes. When it couldn't
                  (a tree too large for one response), that's stated rather than
                  quietly implied — see verification_note in the warnings. */}
              {pubResult.files_verified && (
                <span className="text-emerald-300/80"> · every file confirmed in the repo ✓</span>
              )}
            </div>
            {!!pubResult.repaired_files?.length && (
              <div className="text-amber-300/80">
                {pluralize(pubResult.repaired_files.length, "file")} needed a second commit before
                landing — the repo is complete.
              </div>
            )}
            {pubResult.cicd_added && (
              <div>CI/CD added · {pluralize(pubResult.test_count, "test")} · {pubResult.all_valid ? "all files validated ✓" : "some files need review"}</div>
            )}
          </div>
          {pubResult.warnings?.length > 0 && (
            <div className="pt-1 space-y-1">
              {pubResult.warnings.map((w, i) => (
                <div key={i} className="flex items-start gap-1.5 text-xs text-amber-300/80"><AlertTriangle size={12} className="mt-0.5 shrink-0" />{w}</div>
              ))}
            </div>
          )}

          {/* All three render themselves away when there's nothing to say: no
              suite was generated (CI not requested, or the AI was down and we
              pushed the code anyway), or no credentials were found. An empty
              panel would imply we measured something. */}
          <SuccessRate data={pubResult.success_rate} />
          <ExcludedSecrets files={pubResult.excluded_secrets} where="pushed to the repo" />
          <TrustPanel grounding={pubResult.grounding} provenance={pubResult.provenance} />

          <div className="pt-2 border-t border-grey-700">
            {!confirmingDelete ? (
              <button onClick={() => setConfirmingDelete(true)}
                className="inline-flex items-center gap-1.5 text-xs text-grey-500 hover:text-rose-300 transition-colors">
                <Trash2 size={12} /> Delete this repository
              </button>
            ) : (
              <div className="space-y-2">
                <div className="flex items-start gap-1.5 text-xs text-rose-300">
                  <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                  <span>Permanently delete <span className="font-mono">{pubResult.full_name}</span> from GitHub? This cannot be undone.</span>
                </div>
                {deleteError && <div className="text-xs text-rose-300/80">{deleteError}</div>}
                <div className="flex items-center gap-2">
                  <button onClick={handleDelete} disabled={deleting}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-rose-500/20 border border-rose-500/40 text-xs text-rose-200 hover:bg-rose-500/30 disabled:opacity-50 transition-colors">
                    {deleting ? <><Loader2 size={12} className="animate-spin" /> Deleting...</> : <><Trash2 size={12} /> Yes, delete it</>}
                  </button>
                  <button onClick={() => { setConfirmingDelete(false); setDeleteError(null); }} disabled={deleting}
                    className="px-3 py-1.5 rounded-lg text-xs text-grey-400 hover:text-white/80 disabled:opacity-50 transition-colors">
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {deletedRepo && (
        <div className="px-4 py-3 rounded-xl bg-white/5 border border-grey-700 text-sm text-grey-400">
          Deleted <span className="font-mono">{deletedRepo}</span> from GitHub.
        </div>
      )}

      <div className="space-y-2">
        <button onClick={publish} disabled={publishing || !canPublish}
          className="w-full flex items-center justify-center gap-2 py-3.5 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-sm">
          {publishing ? (<><Loader2 size={16} className="animate-spin" /> {addCicd ? "Generating tests & pushing..." : "Creating repo & pushing..."}</>)
            : (<><Rocket size={16} /> {addCicd ? "Push with CI/CD" : "Push to GitHub"}</>)}
        </button>
        {!publishing && missing.length > 0 && (
          <p className="text-xs text-white/45 text-center">
            Add {missing.join(" and ")} to push.
          </p>
        )}
      </div>

      {/* Below the button, where the eye already is after clicking it. Kept up
          on failure so a partial push is legible; dropped on success, where the
          result card says everything the pipeline would. */}
      {(publishing || (pubError && Object.keys(steps).length > 0)) && (
        <FlowPipeline flow="publish" states={steps} title="Publishing your project" />
      )}
    </div>
  );
}
