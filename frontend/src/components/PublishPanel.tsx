import { useState, useRef, useEffect } from "react";
import {
  Github, Upload, Lock, Loader2, Rocket, CheckCircle2, ExternalLink, AlertTriangle, Trash2,
} from "lucide-react";
import { publishZip, getAuthConfig, githubLoginUrl, validateZip, deleteRepo } from "../utils/api";
import type { PublishResult } from "../types";
import { useAuth } from "../context/AuthContext";
import { celebrate } from "../lib/celebrate";

export default function PublishPanel() {
  const { user } = useAuth();
  const hasGithub = Boolean(user?.has_github);

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
  const [oauthEnabled, setOauthEnabled] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deletedRepo, setDeletedRepo] = useState<string | null>(null);

  useEffect(() => { getAuthConfig().then((c) => setOauthEnabled(c.github_oauth_enabled)).catch(() => {}); }, []);

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
    try {
      const result = await publishZip(file, {
        githubToken: !hasGithub ? ghToken.trim() : undefined,
        repoName: repoName.trim(), addCicd, private: isPrivate,
      });
      setPubResult(result);
      celebrate();
    } catch (e: any) {
      setPubError(e.message || "Publish failed");
    } finally { setPublishing(false); }
  };

  return (
    <div className="w-full max-w-xl mx-auto glass rounded-3xl p-6 shadow-card space-y-5">
      {/* GitHub connection */}
      {hasGithub ? (
        <div className="flex items-center gap-2 px-4 py-3 rounded-2xl bg-emerald-500/10 border border-emerald-500/25 text-sm">
          <Github size={16} className="text-emerald-300" />
          GitHub connected as <span className="font-semibold text-white">{user?.github_login}</span>
        </div>
      ) : oauthEnabled ? (
        <div className="space-y-2">
          <a href={githubLoginUrl()} className="w-full flex items-center justify-center gap-2.5 py-3.5 rounded-2xl bg-black/40 hover:bg-black/60 border border-white/15 text-white font-semibold transition-all text-sm">
            <Github size={17} /> Connect GitHub to publish
          </a>
          <p className="text-xs text-grey-400 text-center">We'll create the repo and push on your behalf.</p>
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
            <div>{pubResult.files_pushed} files pushed to <span className="font-mono">{pubResult.branch}</span></div>
            {pubResult.cicd_added && (
              <div>CI/CD added · {pubResult.test_count} tests · {pubResult.all_valid ? "all files validated ✓" : "some files need review"}</div>
            )}
          </div>
          {pubResult.warnings?.length > 0 && (
            <div className="pt-1 space-y-1">
              {pubResult.warnings.map((w, i) => (
                <div key={i} className="flex items-start gap-1.5 text-xs text-amber-300/80"><AlertTriangle size={12} className="mt-0.5 shrink-0" />{w}</div>
              ))}
            </div>
          )}

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
    </div>
  );
}
