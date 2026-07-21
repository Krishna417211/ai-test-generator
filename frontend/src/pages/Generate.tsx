import { useState, useCallback } from "react";
import { Sparkles } from "lucide-react";
import type { ProjectAnalysis, GenerateResponse, Provenance } from "../types";
import { analyzeRepo, uploadZip, generateTests, streamGeneration, QuotaExceededError } from "../utils/api";
import { formatTokens, pluralize } from "../utils/format";
import UpgradeModal from "../components/UpgradeModal";
import Page from "../components/Page";
import PageHeader from "../components/PageHeader";
import GenerateInput from "../components/GenerateInput";
import PreviewStep from "../components/PreviewStep";
import ConfigureStep from "../components/ConfigureStep";
import StreamingOutput from "../components/StreamingOutput";
import ResultsStep from "../components/ResultsStep";
import StepIndicator from "../components/StepIndicator";
import ProviderStatus from "../components/ProviderStatus";
import FlowPipeline, { applyStep, type StepStates } from "../components/FlowPipeline";
import { celebrate } from "../lib/celebrate";

type Step = "input" | "analyzing" | "preview" | "configure" | "generating" | "done";

export default function Generate() {
  const [step, setStep] = useState<Step>("input");
  const [jobId, setJobId] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<ProjectAnalysis | null>(null);
  // Which model read the repo, shown on the preview screen. Separate from the
  // generate result's own provenance — the two steps can be served by different
  // models, so one value could not honestly describe both.
  const [analysisProvenance, setAnalysisProvenance] = useState<Provenance | null>(null);
  const [result, setResult] = useState<GenerateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [currentProvider, setCurrentProvider] = useState("");
  const [streamOutput, setStreamOutput] = useState("");
  // What the pipeline is showing. Server steps (fetch/extract/agent1) arrive
  // over NDJSON; agent2/scaffold are set below from boundaries the browser can
  // see for itself — never from a timer.
  const [steps, setSteps] = useState<StepStates>({});
  // Set only by a 402 from the server — never by a provider outage.
  const [quotaHit, setQuotaHit] = useState<QuotaExceededError | null>(null);

  const reset = () => {
    setStep("input"); setJobId(null); setAnalysis(null); setAnalysisProvenance(null);
    setResult(null);
    setError(null); setCurrentProvider(""); setStreamOutput(""); setSteps({});
  };

  const handleAnalyze = useCallback(
    async (input: { type: "url"; url: string; token?: string } | { type: "zip"; file: File }) => {
      setStep("analyzing"); setError(null); setSteps({});
      const onProgress = (e: Parameters<typeof applyStep>[1]) =>
        setSteps((s) => applyStep(s, e));
      try {
        const data = input.type === "url"
          ? await analyzeRepo(input.url, "playwright", "typescript", "", "http://localhost:3000", input.token, onProgress)
          : await uploadZip(input.file, "playwright", "typescript", "", "http://localhost:3000", onProgress);
        setJobId(data.job_id); setAnalysis(data.analysis);
        setAnalysisProvenance(data.provenance ?? null);
        setStep("preview");
      } catch (err: any) {
        // Leave the pipeline as it is: the step still spinning is where it
        // broke, which is worth more to the user than a cleared panel.
        setStep("input"); setError(err.message);
      }
    }, []);

  const handleGenerate = useCallback(
    async (config: { framework: string; language: string; testFlows: string; baseUrl: string; includeCi: boolean; liveUrl: string }) => {
      if (!jobId) return;
      // Clear the previous failure before retrying, or a success would render
      // underneath a stale error from the last attempt.
      setError(null);
      setStep("generating"); setStreamOutput(""); setCurrentProvider("");
      // A retry re-runs these two, so clear their old state — but keep the
      // analyze steps, which are still done and still true.
      setSteps((s) => {
        const { agent2: _a, scaffold: _s, ...rest } = s;
        return rest;
      });

      // agent2 and scaffold are client-driven: the SSE opening and the generate
      // request returning are boundaries the browser observes directly, so they
      // need no server events to be honest.
      setSteps((s) => applyStep(s, { id: "agent2", state: "running" }));
      const stop = streamGeneration(
        jobId, config.framework, config.language, config.testFlows, config.baseUrl,
        (chunk) => setStreamOutput((s) => s + chunk),
        (provider) => setCurrentProvider(provider),
        async () => {
          stop();
          setSteps((s) => applyStep(s, { id: "agent2", state: "done", detail: "Suite written" }));
          setSteps((s) => applyStep(s, { id: "scaffold", state: "running" }));
          try {
            const r = await generateTests(jobId, config.framework, config.language, config.testFlows, config.baseUrl, config.includeCi, config.liveUrl);
            // Nothing is streaming any more — leaving this set kept a live
            // "Streaming from groq..." pill on screen next to the finished run.
            setCurrentProvider("");
            setSteps((s) => applyStep(s, {
              id: "scaffold", state: "done",
              detail: `${pluralize(r.test_count, "test")} across ` +
                      `${pluralize(r.files.length, "file")}`,
            }));
            setResult(r); setStep("done"); celebrate();
          } catch (err: any) {
            setStep("configure");
            // Out of personal quota → offer Pro. Anything else (including us
            // being out of AI capacity, which Pro wouldn't fix) is just an error.
            if (err instanceof QuotaExceededError) setQuotaHit(err);
            else setError(err.message);
          }
        },
        (err) => { setStep("configure"); setError(err); },
        // Out of quota, refused before the LLM even runs → same upgrade modal
        // the /api/generate 402 shows, instead of a generic stream error.
        (quotaErr) => { setStep("configure"); setQuotaHit(quotaErr); }
      );
    }, [jobId]);

  return (
    <Page>
      <UpgradeModal quota={quotaHit} onClose={() => setQuotaHit(null)} />
      {step === "input" || step === "analyzing" ? (
        <>
          <PageHeader icon={Sparkles} eyebrow="AI test generation"
            title="Generate production E2E tests"
            subtitle="Point at a GitHub repo or upload a ZIP. Two AI agents filter your code and write runnable tests with real selectors." />
          <GenerateInput onAnalyze={handleAnalyze} loading={step === "analyzing"} error={error} />
          {/* The wait used to be a spinner. This is the same wait, itemized. */}
          {step === "analyzing" && (
            <div className="mx-auto mt-6 max-w-2xl">
              <FlowPipeline flow="generate" states={steps} title="Reading your project" />
            </div>
          )}
        </>
      ) : (
        <div className="flex gap-8 pt-4">
          <div className="flex-1 min-w-0">
            <div className="flex justify-center mb-10"><StepIndicator currentStep={step} /></div>
            {step === "preview" && analysis && <PreviewStep analysis={analysis} provenance={analysisProvenance} onContinue={() => setStep("configure")} />}
            {step === "configure" && analysis && <ConfigureStep detectedFramework={analysis.framework} onGenerate={handleGenerate} loading={false} error={error} />}
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
              {analysis && (
                <div className="glass rounded-2xl p-4 text-xs space-y-2">
                  <div className="text-grey-400 font-semibold uppercase tracking-wider mb-3">Session</div>
                  {[["Framework", analysis.framework], ["Files", String(analysis.file_count)], ["Routes", String(analysis.routes.length)], ["Tokens", formatTokens(analysis.total_tokens)]].map(([k, v]) => (
                    <div key={k} className="flex justify-between"><span className="text-grey-400">{k}</span><span className="text-grey-300">{v}</span></div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </Page>
  );
}
