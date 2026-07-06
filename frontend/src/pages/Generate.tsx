import { useState, useCallback } from "react";
import { Sparkles } from "lucide-react";
import type { ProjectAnalysis, GenerateResponse } from "../types";
import { analyzeRepo, uploadZip, generateTests, streamGeneration } from "../utils/api";
import Page from "../components/Page";
import PageHeader from "../components/PageHeader";
import GenerateInput from "../components/GenerateInput";
import PreviewStep from "../components/PreviewStep";
import ConfigureStep from "../components/ConfigureStep";
import StreamingOutput from "../components/StreamingOutput";
import ResultsStep from "../components/ResultsStep";
import StepIndicator from "../components/StepIndicator";
import ProviderStatus from "../components/ProviderStatus";
import { celebrate } from "../lib/celebrate";

type Step = "input" | "analyzing" | "preview" | "configure" | "generating" | "done";

export default function Generate() {
  const [step, setStep] = useState<Step>("input");
  const [jobId, setJobId] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<ProjectAnalysis | null>(null);
  const [result, setResult] = useState<GenerateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [currentProvider, setCurrentProvider] = useState("");
  const [streamOutput, setStreamOutput] = useState("");

  const reset = () => {
    setStep("input"); setJobId(null); setAnalysis(null); setResult(null);
    setError(null); setCurrentProvider(""); setStreamOutput("");
  };

  const handleAnalyze = useCallback(
    async (input: { type: "url"; url: string; token?: string } | { type: "zip"; file: File }) => {
      setStep("analyzing"); setError(null);
      try {
        const data = input.type === "url"
          ? await analyzeRepo(input.url, "playwright", "typescript", "", "http://localhost:3000", input.token)
          : await uploadZip(input.file, "playwright", "typescript", "", "http://localhost:3000");
        setJobId(data.job_id); setAnalysis(data.analysis); setStep("preview");
      } catch (err: any) {
        setStep("input"); setError(err.message);
      }
    }, []);

  const handleGenerate = useCallback(
    async (config: { framework: string; language: string; testFlows: string; baseUrl: string; includeCi: boolean }) => {
      if (!jobId) return;
      setStep("generating"); setStreamOutput(""); setCurrentProvider("");
      const stop = streamGeneration(
        jobId, config.framework, config.language, config.testFlows, config.baseUrl,
        (chunk) => setStreamOutput((s) => s + chunk),
        (provider) => setCurrentProvider(provider),
        async () => {
          stop();
          try {
            const r = await generateTests(jobId, config.framework, config.language, config.testFlows, config.baseUrl, config.includeCi);
            setResult(r); setStep("done"); celebrate();
          } catch (err: any) { setStep("configure"); setError(err.message); }
        },
        (err) => { setStep("configure"); setError(err); }
      );
    }, [jobId]);

  return (
    <Page>
      {step === "input" || step === "analyzing" ? (
        <>
          <PageHeader icon={Sparkles} eyebrow="AI test generation"
            title="Generate production E2E tests"
            subtitle="Point at a GitHub repo or upload a ZIP. Two AI agents filter your code and write runnable tests with real selectors." />
          <GenerateInput onAnalyze={handleAnalyze} loading={step === "analyzing"} error={error} />
        </>
      ) : (
        <div className="flex gap-8 pt-4">
          <div className="flex-1 min-w-0">
            <div className="flex justify-center mb-10"><StepIndicator currentStep={step} /></div>
            {step === "preview" && analysis && <PreviewStep analysis={analysis} onContinue={() => setStep("configure")} />}
            {step === "configure" && analysis && <ConfigureStep detectedFramework={analysis.framework} onGenerate={handleGenerate} loading={false} />}
            {step === "generating" && <StreamingOutput output={streamOutput} provider={currentProvider} done={false} />}
            {step === "done" && result && <ResultsStep result={result} onReset={reset} />}
          </div>
          <div className="w-64 shrink-0 hidden lg:block">
            <div className="sticky top-24 space-y-4">
              <ProviderStatus currentProvider={currentProvider} />
              {analysis && (
                <div className="glass rounded-2xl p-4 text-xs space-y-2">
                  <div className="text-white/60 font-semibold uppercase tracking-wider mb-3">Session</div>
                  {[["Framework", analysis.framework], ["Files", String(analysis.file_count)], ["Routes", String(analysis.routes.length)], ["Tokens", `~${(analysis.total_tokens / 1000).toFixed(0)}k`]].map(([k, v]) => (
                    <div key={k} className="flex justify-between"><span className="text-white/60">{k}</span><span className="text-white/70">{v}</span></div>
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
