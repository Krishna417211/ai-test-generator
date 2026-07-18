import { Link } from "react-router-dom";
import { TestTube2 } from "lucide-react";

export default function Footer() {
  return (
    <footer className="relative z-10 mt-24 border-t border-white/5">
      <div className="mx-auto max-w-6xl px-6 py-10 flex flex-col sm:flex-row items-center justify-between gap-4">
        <div className="flex items-center gap-2.5 text-white/65">
          <span className="w-6 h-6 rounded-lg bg-brand-gradient flex items-center justify-center">
            <TestTube2 size={12} className="text-ink-950" />
          </span>
          <span className="text-sm">Testra — free & open source</span>
        </div>
        <div className="flex items-center gap-5 text-[13px] text-white/55">
          <Link to="/generate" className="hover:text-white/80 transition-colors">Generate</Link>
          <Link to="/publish" className="hover:text-white/80 transition-colors">Publish</Link>
          <Link to="/scan" className="hover:text-white/80 transition-colors">Scan</Link>
          <Link to="/how-it-works" className="hover:text-white/80 transition-colors">How it works</Link>
          <a href="https://github.com" className="hover:text-white/80 transition-colors">GitHub</a>
        </div>
      </div>
    </footer>
  );
}
