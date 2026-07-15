import { useMemo, useState } from "react";
import { Table2, BarChart3 } from "lucide-react";
import { SERIES } from "../utils/chartPalette";
import type { ActivityDay } from "../types";

/** Chart geometry. A fixed viewBox scaled to the container: the alternative,
 *  measuring the element, buys nothing here — every mark is proportional. */
const W = 720;
const H = 210;
const PAD = { top: 10, right: 8, bottom: 22, left: 30 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;
const MAX_BAR_W = 24;   // spec: cap it — never fill the slot, leave the band air
const GAP = 2;          // spec: 2px surface gap between touching segments
const CAP_R = 4;        // spec: 4px rounded data-end

/** A column segment: rounded at the data-end, square at the baseline. */
function segmentPath(x: number, y: number, w: number, h: number, round: boolean) {
  if (h <= 0) return "";
  if (!round) return `M${x},${y} h${w} v${h} h${-w} Z`;
  const r = Math.min(CAP_R, w / 2, h);
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} `
    + `L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
}

/** Round a max up to a clean axis top so ticks read 0 / 5 / 10, not 0 / 3 / 7. */
function niceMax(max: number): number {
  if (max <= 4) return Math.max(1, max);
  const mag = 10 ** Math.floor(Math.log10(max));
  return Math.ceil(max / (mag / 2)) * (mag / 2);
}

const fmtDay = (iso: string) =>
  new Date(iso + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });

export default function ActivityChart({ series }: { series: ActivityDay[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);

  const { totals, axisMax, ticks, slot, barW } = useMemo(() => {
    const totals = series.map((d) => d.generate + d.publish + d.scan);
    const axisMax = niceMax(Math.max(0, ...totals));
    const step = axisMax <= 4 ? 1 : axisMax / 2;
    const ticks: number[] = [];
    for (let v = 0; v <= axisMax + 1e-9; v += step) ticks.push(Math.round(v));
    const slot = PLOT_W / Math.max(1, series.length);
    return { totals, axisMax, ticks, slot, barW: Math.min(MAX_BAR_W, slot * 0.6) };
  }, [series]);

  const grandTotal = totals.reduce((a, b) => a + b, 0);
  const y = (v: number) => PAD.top + PLOT_H - (v / axisMax) * PLOT_H;

  // The legend is the dependable identity channel and is always present for
  // multiple series; the table view below is the non-visual equivalent.
  const legend = (
    <div className="flex items-center gap-4">
      {SERIES.map((s) => (
        <span key={s.kind} className="flex items-center gap-1.5 text-xs text-grey-400">
          <span className="w-2.5 h-2.5 rounded-[3px]" style={{ background: s.color }} />
          {s.label}
        </span>
      ))}
    </div>
  );

  return (
    <div className="rounded-2xl border border-grey-700 bg-white/[0.03] p-5">
      <div className="flex items-start justify-between gap-4 mb-4 flex-wrap">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-grey-400">
            Activity
          </div>
          <p className="text-xs text-grey-500 mt-1">
            {grandTotal === 0
              ? "The last 30 days"
              : `${grandTotal} action${grandTotal === 1 ? "" : "s"} over the last 30 days`}
          </p>
        </div>
        <div className="flex items-center gap-4">
          {legend}
          <button
            onClick={() => setAsTable((v) => !v)}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg btn-ghost text-xs text-grey-300"
            aria-pressed={asTable}
          >
            {asTable ? <BarChart3 size={13} /> : <Table2 size={13} />}
            {asTable ? "Chart" : "Table"}
          </button>
        </div>
      </div>

      {grandTotal === 0 ? (
        <div className="rounded-xl border border-dashed border-grey-700 px-4 py-10 text-center text-xs text-grey-500">
          Nothing yet. Generate a suite, publish a project, or scan a site and it'll
          chart here.
        </div>
      ) : asTable ? (
        <div className="max-h-[220px] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-ink-900">
              <tr className="text-grey-400 text-left">
                <th className="font-medium py-1.5 pr-2">Day</th>
                {SERIES.map((s) => (
                  <th key={s.kind} className="font-medium py-1.5 px-2 text-right">{s.label}</th>
                ))}
              </tr>
            </thead>
            <tbody className="text-grey-300">
              {series.filter((d) => d.generate + d.publish + d.scan > 0).map((d) => (
                <tr key={d.date} className="border-t border-grey-800">
                  <td className="py-1.5 pr-2 whitespace-nowrap">{fmtDay(d.date)}</td>
                  {SERIES.map((s) => (
                    <td key={s.kind} className="py-1.5 px-2 text-right tabular-nums">
                      {d[s.kind]}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="relative">
          <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto" role="img"
               aria-label={`Daily activity for the last ${series.length} days`}>
            {/* Gridlines + y ticks: hairline, solid, one step off surface. */}
            {ticks.map((t) => (
              <g key={t}>
                <line x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)}
                      stroke="#3a4145" strokeWidth={1} />
                <text x={PAD.left - 6} y={y(t) + 3.5} textAnchor="end"
                      className="fill-grey-500" style={{ fontSize: 9 }}>
                  {t}
                </text>
              </g>
            ))}

            {series.map((d, i) => {
              const x = PAD.left + i * slot + (slot - barW) / 2;
              const stack = SERIES.map((s) => ({ ...s, value: d[s.kind] })).filter((s) => s.value > 0);
              let cursor = PAD.top + PLOT_H;   // grow from a single baseline
              return (
                <g key={d.date}>
                  {stack.map((s, j) => {
                    const full = (s.value / axisMax) * PLOT_H;
                    // The gap is carved out of the segment below the join, so
                    // the stack still totals its true height.
                    const isTop = j === stack.length - 1;
                    const h = Math.max(1, full - (isTop ? 0 : GAP));
                    const top = cursor - full;
                    cursor = top;
                    return (
                      <path key={s.kind} d={segmentPath(x, top, barW, h, isTop)}
                            fill={s.color} opacity={hover === null || hover === i ? 1 : 0.45} />
                    );
                  })}
                  {/* Hit target spans the full plot height — bigger than the mark,
                      so a 1-unit column is still easy to hover. */}
                  <rect x={PAD.left + i * slot} y={PAD.top} width={slot} height={PLOT_H}
                        fill="transparent" onMouseEnter={() => setHover(i)}
                        onMouseLeave={() => setHover(null)} />
                </g>
              );
            })}

            {/* Baseline sits above the marks so caps never bleed past it. */}
            <line x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)}
                  stroke="#545c60" strokeWidth={1} />

            {/* Label only the ends of the axis — a date under all 30 is unreadable. */}
            {series.length > 0 && (
              <>
                <text x={PAD.left} y={H - 6} className="fill-grey-500" style={{ fontSize: 9 }}>
                  {fmtDay(series[0].date)}
                </text>
                <text x={W - PAD.right} y={H - 6} textAnchor="end"
                      className="fill-grey-500" style={{ fontSize: 9 }}>
                  {fmtDay(series[series.length - 1].date)}
                </text>
              </>
            )}
          </svg>

          {hover !== null && totals[hover] > 0 && (
            <div
              className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full
                         rounded-xl border border-grey-600 bg-ink-900 px-3 py-2 shadow-card"
              style={{
                // Clamped: the tooltip is centred on its column, so without this
                // the first and last days push it outside the card and clip it.
                left: `${Math.min(88, Math.max(12,
                  ((PAD.left + hover * slot + slot / 2) / W) * 100))}%`,
                top: `${(y(totals[hover]) / H) * 100}%`,
              }}
            >
              <div className="text-[11px] font-medium text-white whitespace-nowrap">
                {fmtDay(series[hover].date)}
              </div>
              {SERIES.filter((s) => series[hover][s.kind] > 0).map((s) => (
                <div key={s.kind} className="mt-1 flex items-center gap-1.5 whitespace-nowrap">
                  <span className="w-2 h-2 rounded-[2px]" style={{ background: s.color }} />
                  {/* Text stays in ink tokens; the swatch carries identity. */}
                  <span className="text-[11px] text-grey-300">
                    {s.label} · {series[hover][s.kind]}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
