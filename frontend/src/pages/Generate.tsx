import { useState, useCallback } from "react";
import { Sparkles } from "lucide-react";
import type { GenerateResponse } from "../types";
import { analyzeCrawl, generateTests, streamGeneration, QuotaExceededError, type SiteLogin } from "../utils/api";
import UpgradeModal from "../components/UpgradeModal";
import Page from "../components/Page";
import PageHeader from "../components/PageHeader";
import GenerateInput from "../components/GenerateInput";
import ConfigureStep from "../components/ConfigureStep";
import StreamingOutput from "../components/StreamingOutput";
import ResultsStep from "../components/ResultsStep";
import StepIndicator from "../components/StepIndicator";
import ProviderStatus from "../components/ProviderStatus";
import FlowPipeline, { applyStep, type StepStates } from "../components/FlowPipeline";
import { celebrate } from "../lib/celebrate";

type Step = "input" | "configure" | "generating" | "done";

export default function Generate() {
  const [step, setStep] = useState<Step>("input");
  // Collected in the input step; the hosted URL is the site the suite is written
  // from, the repo URL confirms access and is where the suite publishes back.
  const [repoUrl, setRepoUrl] = useState("");
  const [hostedUrl, setHostedUrl] = useState("");
  // Kept in memory for the run only — deliberately not persisted anywhere.
  const [siteLogin, setSiteLogin] = useState<SiteLogin | null>(null);
  const [result, setResult] = useState<GenerateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [currentProvider, setCurrentProvider] = useState("");
  const [streamOutput, setStreamOutput] = useState("");
  const [steps, setSteps] = useState<StepStates>({});
  const [quotaHit, setQuotaHit] = useState<QuotaExceededError | null>(null);

  const reset = () => {
    setStep("input"); setResult(null); setError(null);
    setCurrentProvider(""); setStreamOutput(""); setSteps({});
    setSiteLogin(null);          // credentials never outlive the run that used them
  };

  // Input step → just capture the two URLs and advance. No backend call yet; the
  // crawl runs at generate time so the framework/language choice is made first.
  const handleContinue = useCallback(
    (input: { repoUrl: string; hostedUrl: string; siteLogin: SiteLogin | null }) => {
      setRepoUrl(input.repoUrl);
      setHostedUrl(input.hostedUrl);
      setSiteLogin(input.siteLogin);
      setError(null);
      setStep("configure");
    }, []);

  const handleGenerate = useCallback(
    async (config: { framework: string; language: string; testFlows: string; includeCi: boolean }) => {
      setError(null);
      setStep("generating");
      setStreamOutput(""); setCurrentProvider(""); setSteps({});

      // 1) Crawl the hosted site and create the job. This renders the real DOM the
      //    AI writes from; a crawl failure stops here with an honest reason —
      //    there is no repo-file fallback.
      setSteps((s) => applyStep(s, { id: "crawl", state: "running" }));
      let job: string;
      try {
        const a = await analyzeCrawl(repoUrl, hostedUrl, config.framework, config.language, config.testFlows, siteLogin);
        job = a.job_id;
        setSteps((s) => applyStep(s, {
          id: "crawl", state: "done",
          detail: a.crawl ? `${a.crawl.n_pages} routes · ${a.crawl.n_anchors} anchors` : "site rendered",
        }));
      } catch (err: any) {
        setStep("configure");
        if (err instanceof QuotaExceededError) setQuotaHit(err);
        else setError(err.message);
        return;
      }

      // 2) Stream the suite (agent2), then finalize + scaffold (baking the hosted
      //    URL into a single runnable suite).
      setSteps((s) => applyStep(s, { id: "agent2", state: "running" }));
      const stop = streamGeneration(
        job, config.framework, config.language, config.testFlows, hostedUrl,
        (chunk) => setStreamOutput((s) => s + chunk),
        (provider) => setCurrentProvider(provider),
        async () => {
          stop();
          setSteps((s) => applyStep(s, { id: "agent2", state: "done", detail: "Suite written" }));
          setSteps((s) => applyStep(s, { id: "scaffold", state: "running" }));
          try {
            const r = await generateTests(job, config.framework, config.language, config.testFlows, hostedUrl, config.includeCi, hostedUrl);
            setCurrentProvider("");
            setSteps((s) => applyStep(s, {
              id: "scaffold", state: "done",
              detail: `${r.test_count} tests across ${r.files.length} files`,
            }));
            setResult(r); setStep("done"); celebrate();
          } catch (err: any) {
            setStep("configure");
            if (err instanceof QuotaExceededError) setQuotaHit(err);
            else setError(err.message);
          }
        },
        (err) => { setStep("configure"); setError(err); },
        (quotaErr) => { setStep("configure"); setQuotaHit(quotaErr); }
      );
    }, [repoUrl, hostedUrl, siteLogin]);

  return (
    <Page>
      <UpgradeModal quota={quotaHit} onClose={() => setQuotaHit(null)} />
      {step === "input" ? (
        <>
          <PageHeader icon={Sparkles} eyebrow="AI test generation"
            title="Generate production E2E tests"
            subtitle="Point at your GitHub repo and your live site. Testra crawls the deployed app and writes runnable tests from its real elements." />
          <GenerateInput onContinue={handleContinue} loading={false} error={error} />
        </>
      ) : (
        <div className="flex gap-8 pt-4">
          <div className="flex-1 min-w-0">
            <div className="flex justify-center mb-10"><StepIndicator currentStep={step} /></div>
            {step === "configure" && <ConfigureStep hostedUrl={hostedUrl} siteLogin={siteLogin} onGenerate={handleGenerate} loading={false} error={error} />}
            {step === "generating" && (
              <div className="space-y-5">
                <FlowPipeline flow="generate" states={steps} title="Writing your tests" />
                <StreamingOutput output={streamOutput} provider={currentProvider} done={false} />
              </div>
            )}
            {step === "done" && result && <ResultsStep result={result} onReset={reset} />}
          </div>
          <div className="w-64 shrink-0 hidden lg:block">
            <div className="sticky top-24 space-y-4">
              <ProviderStatus currentProvider={currentProvider} />
              <div className="glass rounded-2xl p-4 text-xs space-y-2">
                <div className="text-grey-400 font-semibold uppercase tracking-wider mb-3">Session</div>
                {[["Repo", repoUrl.replace(/^https?:\/\/(www\.)?github\.com\//, "")], ["Live site", hostedUrl.replace(/^https?:\/\//, "")]].map(([k, v]) => (
                  <div key={k} className="flex justify-between gap-3"><span className="text-grey-400">{k}</span><span className="text-grey-300 truncate max-w-[140px]">{v}</span></div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </Page>
  );
}
