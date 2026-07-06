/** Ambient animated aurora + grid backdrop, fixed behind all content. */
export default function AuroraBackground() {
  return (
    <div className="fixed inset-0 -z-10 overflow-hidden bg-ink-950 pointer-events-none">
      {/* Aurora blobs — neon fuchsia · violet · cyan (kept toward the edges) */}
      <div className="absolute -top-56 -left-44 w-[40rem] h-[40rem] rounded-full blur-[140px] bg-fuchsia-500/22 animate-aurora" />
      <div className="absolute top-1/4 -right-52 w-[38rem] h-[38rem] rounded-full blur-[140px] bg-cyanx-400/16 animate-auroraB" />
      <div className="absolute -bottom-60 left-1/5 w-[40rem] h-[40rem] rounded-full blur-[150px] bg-brand-500/20 animate-aurora" style={{ animationDelay: "-6s" }} />

      {/* Fine grid (subtle) */}
      <div
        className="absolute inset-0 bg-grid opacity-40"
        style={{ backgroundSize: "56px 56px", maskImage: "radial-gradient(120% 80% at 50% 0%, black 35%, transparent 80%)", WebkitMaskImage: "radial-gradient(120% 80% at 50% 0%, black 35%, transparent 80%)" }}
      />

      {/* Readability scrim — darkens the center so text stays crisp over the aurora */}
      <div className="absolute inset-0" style={{ background: "radial-gradient(100% 70% at 50% 40%, rgba(28,37,41,0.72), rgba(28,37,41,0.35) 70%, transparent)" }} />

      {/* Top sheen + bottom fade */}
      <div className="absolute inset-0" style={{ background: "radial-gradient(120% 90% at 50% -10%, rgba(161,209,177,0.12), transparent 55%)" }} />
      <div className="absolute inset-x-0 bottom-0 h-64 bg-gradient-to-t from-ink-950 to-transparent" />

      {/* Noise */}
      <div
        className="absolute inset-0 opacity-[0.035] mix-blend-soft-light"
        style={{ backgroundImage: "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E\")" }}
      />
    </div>
  );
}
