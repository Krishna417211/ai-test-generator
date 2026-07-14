import { useEffect, useState, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, Check, Zap, Loader2, AlertCircle } from "lucide-react";
import { startCheckout, CheckoutUnavailableError } from "../utils/api";
import type { QuotaExceededError } from "../utils/api";

type PlanId = "monthly" | "yearly";

const PERKS = [
  "Unlimited test generations",
  "Priority access when capacity is tight",
  "Page Object Models + CI/CD pipelines",
  "Private repo publishing",
];

function resetLabel(resetsAt: number): string {
  if (!resetsAt) return "next month";
  return new Date(resetsAt * 1000).toLocaleDateString(undefined, {
    month: "long",
    day: "numeric",
  });
}

export default function UpgradeModal({
  quota,
  onClose,
}: {
  quota: QuotaExceededError | null;
  onClose: () => void;
}) {
  const [selected, setSelected] = useState<PlanId>("yearly");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  const open = quota !== null;

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    closeRef.current?.focus();
    // Stop the page behind the overlay from scrolling under it.
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  useEffect(() => {
    if (open) setError(null);
  }, [open]);

  const handleUpgrade = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const url = await startCheckout(selected);
      window.location.href = url;
    } catch (err: any) {
      setError(
        err instanceof CheckoutUnavailableError
          ? "Checkout isn't live yet — we can't take payments through the app right now."
          : err.message || "Could not start checkout.",
      );
      setLoading(false);
    }
  }, [selected]);

  const monthly = quota?.pricing.monthly_usd ?? 20;
  const yearly = quota?.pricing.yearly_usd ?? 100;
  const savings = Math.max(0, monthly * 12 - yearly);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
        >
          <div
            className="absolute inset-0 bg-ink-950/80 backdrop-blur-sm"
            onClick={onClose}
            aria-hidden="true"
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-labelledby="upgrade-title"
            className="glass-strong relative w-full max-w-md rounded-3xl p-7 shadow-card max-h-[90vh] overflow-y-auto"
            initial={{ opacity: 0, y: 16, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 16, scale: 0.97 }}
            transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
          >
            <button
              ref={closeRef}
              onClick={onClose}
              aria-label="Close"
              className="absolute right-4 top-4 rounded-lg p-1.5 text-grey-400 hover:text-white/90 hover:bg-white/5 transition"
            >
              <X size={18} />
            </button>

            <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full glass text-xs text-brand-200 mb-4">
              <Zap size={13} /> Free limit reached
            </div>

            <h2 id="upgrade-title" className="font-display text-2xl font-bold tracking-tight">
              Upgrade to <span className="text-gradient">Testra Pro</span>
            </h2>
            <p className="mt-2 text-sm text-grey-300 leading-relaxed">
              You've used all {quota?.limit ?? 5} free generations this month. Your
              quota resets on {resetLabel(quota?.resetsAt ?? 0)} — or go unlimited now.
            </p>

            <div className="mt-6 space-y-2.5">
              {(
                [
                  { id: "yearly" as PlanId, price: yearly, unit: "/year", note: savings > 0 ? `Save $${savings}` : "" },
                  { id: "monthly" as PlanId, price: monthly, unit: "/month", note: "" },
                ]
              ).map((p) => (
                <button
                  key={p.id}
                  onClick={() => setSelected(p.id)}
                  aria-pressed={selected === p.id}
                  className={`w-full flex items-center justify-between rounded-2xl border p-4 text-left transition ${
                    selected === p.id
                      ? "border-brand-400/60 bg-brand-400/10 shadow-glow"
                      : "border-grey-700 bg-white/[0.02] hover:border-white/20"
                  }`}
                >
                  <span className="flex items-center gap-3">
                    <span
                      className={`flex h-4 w-4 items-center justify-center rounded-full border ${
                        selected === p.id ? "border-brand-400 bg-brand-400" : "border-white/25"
                      }`}
                    >
                      {selected === p.id && <Check size={10} className="text-ink-950" strokeWidth={3.5} />}
                    </span>
                    <span className="font-semibold capitalize">{p.id}</span>
                    {p.note && (
                      <span className="rounded-full bg-brand-400/15 px-2 py-0.5 text-[11px] font-medium text-brand-200">
                        {p.note}
                      </span>
                    )}
                  </span>
                  <span className="font-display text-lg font-bold">
                    ${p.price}
                    <span className="text-xs font-normal text-grey-400">{p.unit}</span>
                  </span>
                </button>
              ))}
            </div>

            <ul className="mt-6 space-y-2">
              {PERKS.map((perk) => (
                <li key={perk} className="flex items-center gap-2.5 text-sm text-white/75">
                  <Check size={14} className="shrink-0 text-brand-400" strokeWidth={3} />
                  {perk}
                </li>
              ))}
            </ul>

            {error && (
              <div
                role="alert"
                className="mt-5 flex items-start gap-2.5 rounded-xl border border-amber-400/30 bg-amber-400/10 p-3 text-xs leading-relaxed text-amber-400"
              >
                <AlertCircle size={14} className="mt-0.5 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <button
              onClick={handleUpgrade}
              disabled={loading}
              className="btn-primary mt-6 flex w-full items-center justify-center gap-2 rounded-xl py-3 font-semibold disabled:opacity-60"
            >
              {loading ? (
                <>
                  <Loader2 size={16} className="animate-spin" /> Starting checkout…
                </>
              ) : (
                <>Upgrade — ${selected === "yearly" ? yearly : monthly}{selected === "yearly" ? "/yr" : "/mo"}</>
              )}
            </button>

            <button
              onClick={onClose}
              className="mt-3 w-full py-2 text-xs text-grey-400 transition hover:text-white/80"
            >
              Not now — I'll wait for my quota to reset
            </button>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
