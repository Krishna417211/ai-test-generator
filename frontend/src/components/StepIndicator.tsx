import { Check } from "lucide-react";

type Step = "input" | "analyzing" | "preview" | "configure" | "generating" | "done";

const STEPS: { id: Step; label: string }[] = [
  { id: "input", label: "Input" },
  { id: "preview", label: "Preview" },
  { id: "configure", label: "Configure" },
  { id: "done", label: "Download" },
];

function getVisualIndex(step: Step): number {
  // Map wizard steps to visual step indices
  if (step === "input" || step === "analyzing") return 0;
  if (step === "preview") return 1;
  if (step === "configure") return 2;
  if (step === "generating" || step === "done") return 3;
  return 0;
}

interface Props {
  currentStep: Step;
}

export default function StepIndicator({ currentStep }: Props) {
  const current = getVisualIndex(currentStep);

  return (
    <div className="flex items-center gap-0">
      {STEPS.map((step, i) => {
        const done = i < current;
        const active = i === current;

        return (
          <div key={step.id} className="flex items-center">
            <div className="flex flex-col items-center gap-1.5">
              <div
                className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold border-2 transition-all ${
                  done
                    ? "bg-brand-400 border-brand-400 text-ink-950"
                    : active
                    ? "bg-white/[0.06] border-grey-300 text-white"
                    : "bg-transparent border-grey-700 text-grey-600"
                }`}
              >
                {done ? <Check size={13} /> : <span>{i + 1}</span>}
              </div>
              <span
                className={`text-xs font-medium transition-colors ${
                  active ? "text-white" : done ? "text-grey-400" : "text-grey-600"
                }`}
              >
                {step.label}
              </span>
            </div>

            {i < STEPS.length - 1 && (
              <div
                className={`w-16 h-px mx-2 mb-5 transition-colors ${
                  i < current ? "bg-brand-400/50" : "bg-grey-700"
                }`}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
