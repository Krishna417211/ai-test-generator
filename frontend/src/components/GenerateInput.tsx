import { useState, useRef } from "react";
import { Github, Upload, ArrowRight, Lock, Loader2 } from "lucide-react";

interface Props {
  onAnalyze: (input: { type: "url"; url: string; token?: string } | { type: "zip"; file: File }) => void;
  loading: boolean;
  error: string | null;
}

export default function GenerateInput({ onAnalyze, loading, error }: Props) {
  const [mode, setMode] = useState<"url" | "zip">("url");
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const submit = () => {
    if (mode === "url") {
      if (!url.trim()) return;
      onAnalyze({ type: "url", url: url.trim(), token: token || undefined });
    } else if (file) {
      onAnalyze({ type: "zip", file });
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const dropped = e.dataTransfer.files[0];
    if (dropped?.name.endsWith(".zip")) setFile(dropped);
  };

  return (
    <div className="w-full max-w-xl mx-auto">
      <div className="glass rounded-3xl p-2 shadow-card">
        <div className="flex rounded-2xl bg-black/20 p-1 mb-1">
          {[
            { id: "url", label: "GitHub URL", icon: Github },
            { id: "zip", label: "Upload ZIP", icon: Upload },
          ].map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setMode(id as "url" | "zip")}
              className={`flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-medium transition-all ${
                mode === id ? "bg-white/10 text-white shadow-inner-hi" : "text-white/65 hover:text-white/80"
              }`}
            >
              <Icon size={15} /> {label}
            </button>
          ))}
        </div>

        <div className="p-4">
          {mode === "url" ? (
            <div className="space-y-3">
              <div className="relative">
                <Github size={16} className="absolute left-4 top-1/2 -translate-y-1/2 text-white/40" />
                <input
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && submit()}
                  placeholder="https://github.com/owner/repo"
                  className="w-full pl-10 pr-4 py-3.5 rounded-xl bg-black/30 border border-white/10 text-white placeholder:text-white/45 focus:outline-none focus:border-brand-500/70 focus:ring-2 focus:ring-brand-500/20 transition-all text-sm"
                />
              </div>
              <button onClick={() => setShowToken(!showToken)} className="flex items-center gap-1.5 text-xs text-white/40 hover:text-white/60 transition-colors">
                <Lock size={12} /> {showToken ? "Hide" : "Private repo?"} Add GitHub token
              </button>
              {showToken && (
                <input
                  type="password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder="ghp_xxxxxxxxxxxx"
                  className="w-full px-4 py-3 rounded-xl bg-black/30 border border-white/10 text-white placeholder:text-white/45 focus:outline-none focus:border-brand-500/70 transition-colors text-sm font-mono"
                />
              )}
            </div>
          ) : (
            <div
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={handleDrop}
              onClick={() => fileRef.current?.click()}
              className={`w-full h-44 rounded-2xl border-2 border-dashed flex flex-col items-center justify-center gap-3 cursor-pointer transition-all ${
                dragOver ? "border-brand-400 bg-brand-500/10 scale-[1.01]"
                  : file ? "border-emerald-500/50 bg-emerald-500/10"
                  : "border-white/15 bg-black/20 hover:border-white/30"
              }`}
            >
              <div className={`w-12 h-12 rounded-2xl flex items-center justify-center ${file ? "bg-emerald-500/20" : "bg-white/5"}`}>
                <Upload size={22} className={file ? "text-emerald-400" : "text-white/40"} />
              </div>
              <div className="text-center">
                <p className="text-sm text-white/75">{file ? file.name : "Drop your repo ZIP here"}</p>
                <p className="text-xs text-white/30 mt-1">{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB` : "or click to browse"}</p>
              </div>
              <input ref={fileRef} type="file" accept=".zip" className="hidden" onChange={(e) => e.target.files?.[0] && setFile(e.target.files[0])} />
            </div>
          )}

          {error && (
            <div className="mt-4 px-4 py-3 rounded-xl bg-rose-500/15 border border-rose-500/30 text-sm text-rose-300">{error}</div>
          )}

          <button
            onClick={submit}
            disabled={loading || (mode === "url" ? !url.trim() : !file)}
            className="mt-4 w-full flex items-center justify-center gap-2 py-3.5 rounded-xl btn-primary disabled:opacity-40 disabled:cursor-not-allowed font-semibold text-sm"
          >
            {loading ? (<><Loader2 size={16} className="animate-spin" /> Analyzing repository...</>)
              : (<>Analyze repository <ArrowRight size={16} /></>)}
          </button>
        </div>
      </div>
    </div>
  );
}
