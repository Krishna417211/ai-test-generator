import { ShieldCheck } from "lucide-react";
import Page from "../components/Page";
import PageHeader from "../components/PageHeader";
import ScanPanel from "../components/ScanPanel";

export default function Scan() {
  return (
    <Page>
      <PageHeader icon={ShieldCheck} eyebrow="Production security audit"
        title="Scan your deployed app"
        subtitle="Find the misconfigurations that bite in production — HTTPS, security headers, cookies, CORS and exposed files — each with a concrete fix." />
      <div className="max-w-2xl mx-auto glass rounded-3xl p-6 shadow-card">
        <ScanPanel />
      </div>
    </Page>
  );
}
