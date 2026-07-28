import { Rocket, GitBranch, ShieldCheck, Zap, Terminal } from "lucide-react";
import { Link } from "react-router-dom";
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
              <div className="w-10 h-10 rounded-xl bg-iris-500/15 border border-grey-700 flex items-center justify-center mb-3">
                <p.icon size={18} className="text-iris-400" />
              </div>
              <h3 className="font-display font-semibold text-sm mb-1">{p.title}</h3>
              <p className="text-xs text-grey-400 leading-relaxed">{p.desc}</p>
            </div>
          ))}

          {/* Shown here rather than only in Settings: this is the page where
              someone is about to hand-make a ZIP, which is exactly the step the
              CLI removes. */}
          <div className="glass rounded-2xl p-5">
            <div className="w-10 h-10 rounded-xl bg-white/[0.06] border border-grey-700 flex items-center justify-center mb-3">
              <Terminal size={18} className="text-grey-300" />
            </div>
            <h3 className="font-display font-semibold text-sm mb-1">Skip the ZIP</h3>
            <p className="text-xs text-grey-400 leading-relaxed mb-3">
              Run it from the project directory instead. Your <code className="font-mono text-grey-300">.gitignore</code>{" "}
              decides what uploads, and credentials are found before anything is sent.
            </p>
            <code className="block rounded-lg bg-black/40 border border-grey-700 px-2.5 py-2 text-[11px] font-mono text-grey-200 overflow-x-auto">
              npx testra-cli publish --with-ci
            </code>
            <Link to="/settings" className="inline-block mt-3 text-xs text-brand-300 hover:text-brand-200">
              How to sign in →
            </Link>
          </div>
        </div>
      </div>
    </Page>
  );
}
