import type { LucideIcon } from "lucide-react";

export default function PageHeader({ icon: Icon, eyebrow, title, subtitle }: {
  icon: LucideIcon; eyebrow: string; title: string; subtitle: string;
}) {
  return (
    <div className="text-center max-w-2xl mx-auto mb-10 pt-6">
      <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full glass text-xs text-brand-200 mb-5">
        <Icon size={13} /> {eyebrow}
      </div>
      <h1 className="font-display text-3xl sm:text-4xl font-bold tracking-tight">{title}</h1>
      <p className="mt-3 text-white/70 leading-relaxed">{subtitle}</p>
    </div>
  );
}
