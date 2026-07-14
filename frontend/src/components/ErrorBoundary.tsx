import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

interface Props { children: ReactNode }
interface State { error: Error | null }

/**
 * Catches render/runtime errors anywhere below it (e.g. a mid-stream failure
 * in the generate flow) so a single thrown error can't blank the whole app.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surface it for debugging; wire to an error tracker (Sentry) here later.
    console.error("Uncaught UI error:", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="min-h-[70vh] flex items-center justify-center px-4">
        <div className="glass rounded-3xl p-10 text-center max-w-md">
          <div className="w-14 h-14 rounded-2xl bg-rose-500/15 border border-rose-500/25 flex items-center justify-center mx-auto mb-5">
            <AlertTriangle size={26} className="text-rose-400" />
          </div>
          <h1 className="font-display text-2xl font-bold">Something went wrong</h1>
          <p className="mt-3 text-white/70 text-sm">
            An unexpected error interrupted the page. Reloading usually fixes it.
          </p>
          <button
            onClick={() => { this.setState({ error: null }); window.location.reload(); }}
            className="mt-7 inline-flex items-center gap-2 px-6 py-3 rounded-2xl btn-primary font-semibold text-sm"
          >
            <RotateCcw size={16} /> Reload
          </button>
        </div>
      </div>
    );
  }
}
