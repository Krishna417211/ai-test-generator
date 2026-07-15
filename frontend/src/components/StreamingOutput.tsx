import { useEffect, useRef } from "react";
import { Loader2, Zap } from "lucide-react";

interface Props {
  output: string;
  provider: string;
  done: boolean;
}

export default function StreamingOutput({ output, provider, done }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [output]);

  return (
    <div className="w-full max-w-3xl mx-auto">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          {!done && <Loader2 size={14} className="animate-spin text-grey-400" />}
          <span className="text-sm font-semibold text-white">
            {done ? "Generation complete" : "Generating tests..."}
          </span>
        </div>
        {provider && (
          <div className="flex items-center gap-1.5 text-xs text-grey-300 bg-white/[0.06] px-3 py-1 rounded-full border border-grey-600">
            <Zap size={11} className="text-grey-400" />
            {provider}
          </div>
        )}
      </div>

      <div className="rounded-xl border border-grey-700 bg-black/30 overflow-hidden">
        <div className="flex items-center gap-1.5 px-4 py-2.5 border-b border-grey-700 bg-white/3">
          <div className="w-3 h-3 rounded-full bg-red-500/60" />
          <div className="w-3 h-3 rounded-full bg-yellow-500/60" />
          <div className="w-3 h-3 rounded-full bg-green-500/60" />
          <span className="ml-2 text-xs text-grey-500 font-mono">output</span>
        </div>
        <pre className="p-4 text-xs text-brand-200/90 font-mono leading-relaxed max-h-[500px] overflow-y-auto whitespace-pre-wrap">
          {output || <span className="text-grey-600">Waiting for output...</span>}
          {!done && <span className="inline-block w-2 h-4 bg-brand-400 ml-0.5 animate-pulse align-middle" />}
          <div ref={bottomRef} />
        </pre>
      </div>
    </div>
  );
}
