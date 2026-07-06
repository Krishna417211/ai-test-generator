import { Rocket, GitBranch, ShieldCheck, Zap } from "lucide-react";
import Page from "../components/Page";
import PageHeader from "../components/PageHeader";
import PublishPanel from "../components/PublishPanel";

const PERKS = [
  { icon: GitBranch, title: "Fresh repo, one click", desc: "We create the repo and push every file in a single clean commit." },
  { icon: ShieldCheck, title: "Validated CI/CD", desc: "Optional E2E suite is generated and checked locally before it lands." },
  { icon: Zap, title: "No token juggling", desc: "Log in with GitHub — we push on your behalf, securely." },
];

export default function Publish() {
  return (
    <Page>
      <PageHeader icon={Rocket} eyebrow="One-click publishing"
        title="Push your project to GitHub"
        subtitle="Log in with GitHub, drop a ZIP, and we spin up a brand-new repo — with a validated CI/CD pipeline if you want one." />
      <div className="grid lg:grid-cols-[1fr_320px] gap-8 items-start">
        <PublishPanel />
        <div className="space-y-3">
          {PERKS.map((p) => (
            <div key={p.title} className="glass rounded-2xl p-5">
              <div className="w-10 h-10 rounded-xl bg-iris-500/15 border border-white/10 flex items-center justify-center mb-3">
                <p.icon size={18} className="text-iris-400" />
              </div>
              <h3 className="font-display font-semibold text-sm mb-1">{p.title}</h3>
              <p className="text-xs text-white/50 leading-relaxed">{p.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </Page>
  );
}
