import { Link } from "react-router-dom";
import { Compass, ArrowLeft } from "lucide-react";
import Page from "../components/Page";

export default function NotFound() {
  return (
    <Page className="flex items-center justify-center min-h-[70vh]">
      <div className="glass rounded-3xl p-10 text-center max-w-md">
        <div className="w-14 h-14 rounded-2xl bg-brand-gradient/20 border border-grey-700 flex items-center justify-center mx-auto mb-5">
          <Compass size={26} className="text-brand-300" />
        </div>
        <h1 className="font-display text-5xl font-bold text-gradient">404</h1>
        <p className="mt-3 text-grey-300">
          This page took a wrong turn. The link may be broken or the page may have moved.
        </p>
        <Link to="/" className="mt-7 inline-flex items-center gap-2 px-6 py-3 rounded-2xl btn-primary font-semibold text-sm">
          <ArrowLeft size={16} /> Back home
        </Link>
      </div>
    </Page>
  );
}
