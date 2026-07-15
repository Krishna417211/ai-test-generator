/**
 * Categorical palette for dashboard charts — one slot per feature.
 *
 * These are NOT the UI's `brand`/`progress`/`amber` tokens, and that is
 * deliberate. Those tokens are pastel accents built for text and soft fills:
 * measured in OKLCH they sit at chroma ~0.06–0.09, under the 0.10 floor at which
 * a hue stops reading as identity and starts reading as gray, and at lightness
 * ~0.72–0.82, above the 0.48–0.67 band a mark needs against our dark surface.
 * Using them as series colors fails on both counts — and picking three from the
 * sage family (brand/fuchsia/emerald are near-identical hues) puts two series at
 * ΔE 3.5 for *normal* vision, i.e. nobody can tell them apart.
 *
 * So these are chart-specific steps on the same three hue families the design
 * system already uses (sage green, lavender, gold), pushed down in lightness and
 * up in chroma until every check passes. They were found by search, not by eye,
 * and verified against the surface below:
 *
 *   lightness band  PASS   all inside L 0.48–0.67 (dark) / 0.43–0.77 (light)
 *   chroma floor    PASS   all >= 0.10
 *   CVD separation  PASS   worst all-pairs ΔE 10.9 (deutan) — target is >= 8
 *   normal vision   PASS   worst all-pairs ΔE 19.5 — floor is 15
 *   contrast        PASS   all >= 3:1 vs surface
 *
 * Order is fixed and assigned by feature, never cycled: a chart that drops a
 * series must not repaint the survivors.
 */

/** The composited chart surface: `bg-white/[0.03]` over `ink-950` (#1c2529). */
export const CHART_SURFACE = "#232b2f";

export type ActivityKind = "generate" | "publish" | "scan";

export const SERIES: { kind: ActivityKind; label: string; color: string }[] = [
  { kind: "generate", label: "Generate", color: "#1ba17c" },
  { kind: "publish", label: "Publish", color: "#7471b3" },
  { kind: "scan", label: "Scan", color: "#a96a10" },
];

export const SERIES_COLOR: Record<ActivityKind, string> = {
  generate: "#1ba17c",
  publish: "#7471b3",
  scan: "#a96a10",
};
