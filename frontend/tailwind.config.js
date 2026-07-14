/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        // Clean sans for all UI/headings; script for decorative accent words.
        display: ['"Montserrat"', "system-ui", "sans-serif"],
        sans: ['"Montserrat"', "system-ui", "-apple-system", "sans-serif"],
        // Great Vibes (Google Fonts) → cursive.
        script: ['"Great Vibes"', "cursive"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      colors: {
        // Dark slate base (#1C2529)
        ink: {
          950: "#1c2529",
          900: "#212b30",
          850: "#283339",
          800: "#303c43",
        },
        // Sage green (#A1D1B1) — the primary accent (kept the `brand` name)
        brand: {
          50: "#eef7f1",
          200: "#cbe6d5",
          300: "#b6dcc3",
          400: "#a1d1b1",
          500: "#8ac6a0",
          600: "#6fb188",
          700: "#57956f",
        },
        // Deeper sage-teal — a richer accent variant
        fuchsia: {
          200: "#bce3ce",
          300: "#a8d8c0",
          400: "#8cc9aa",
          500: "#6fb891",
          600: "#56a078",
        },
        // Slate gray-green — for depth against the sage
        iris: { 400: "#7a8f88", 500: "#5f746d", 600: "#4a5b55" },
        // Neutral grey — secondary surfaces, borders, muted text. Cool rather
        // than a true 50% grey: a neutral #808080 reads warm and dirty against
        // the cool ink base, so these carry a slight blue bias to sit with it
        // without pulling back toward the sage.
        grey: {
          200: "#dcdfe1",
          300: "#c9ced1",  // 9.8:1 on ink-950
          400: "#9aa3a8",  // 6.1:1
          // 4.5:1 — the floor for helper text ("(optional)", file sizes, hints),
          // which is what this tier is used for. Anything darker reads as
          // decoration and fails WCAG AA for body copy.
          500: "#818c92",
          600: "#545c60",  // non-text only: borders, dividers
          700: "#3a4145",
          800: "#2a3034",
        },
        // Pale mint — the light end of gradients
        cyanx: { 300: "#e6f3ec", 400: "#c9e6d4", 500: "#a9d4bb" },
        // Functional semantic accents (kept for status/severity signals)
        emerald: { 400: "#7fb39a", 500: "#5f9c80" },
        amber: { 400: "#e0b877", 500: "#c99a4f" },
        rose: { 400: "#d98a86", 500: "#c56b66" },
        // Muted lavender — "in progress" state (replaces raw Tailwind violet)
        progress: { 300: "#cdc7e0", 400: "#a99fc9", 500: "#8f82b8", 600: "#7568a0" },
      },
      boxShadow: {
        glow: "0 0 0 1px rgba(161,209,177,0.25), 0 8px 44px -10px rgba(161,209,177,0.4)",
        "glow-cyan": "0 0 0 1px rgba(201,230,212,0.22), 0 8px 40px -8px rgba(201,230,212,0.35)",
        card: "0 1px 0 0 rgba(255,255,255,0.04) inset, 0 20px 50px -20px rgba(0,0,0,0.65)",
        "inner-hi": "0 1px 0 0 rgba(255,255,255,0.06) inset",
      },
      backgroundImage: {
        "brand-gradient": "linear-gradient(120deg,#cbe6d5 0%,#a1d1b1 50%,#8ac6a0 100%)",
        "brand-radial": "radial-gradient(80% 80% at 50% 0%,rgba(161,209,177,0.22),transparent 60%)",
        grid: "linear-gradient(rgba(255,255,255,0.03) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,0.03) 1px,transparent 1px)",
      },
      keyframes: {
        aurora: {
          "0%,100%": { transform: "translate3d(0,0,0) scale(1)", opacity: "0.7" },
          "50%": { transform: "translate3d(4%,-3%,0) scale(1.15)", opacity: "1" },
        },
        auroraB: {
          "0%,100%": { transform: "translate3d(0,0,0) scale(1.1)", opacity: "0.6" },
          "50%": { transform: "translate3d(-5%,4%,0) scale(1)", opacity: "0.9" },
        },
        float: { "0%,100%": { transform: "translateY(0)" }, "50%": { transform: "translateY(-8px)" } },
        shimmer: { "100%": { transform: "translateX(100%)" } },
        gradientmove: { "0%,100%": { backgroundPosition: "0% 50%" }, "50%": { backgroundPosition: "100% 50%" } },
        "pulse-glow": {
          "0%,100%": { boxShadow: "0 0 0 0 rgba(161,209,177,0.5)" },
          "50%": { boxShadow: "0 0 0 10px rgba(161,209,177,0)" },
        },
        "fade-up": { "0%": { opacity: "0", transform: "translateY(12px)" }, "100%": { opacity: "1", transform: "translateY(0)" } },
        marquee: { "0%": { transform: "translateX(0)" }, "100%": { transform: "translateX(-50%)" } },
        spinslow: { to: { transform: "rotate(360deg)" } },
      },
      animation: {
        aurora: "aurora 16s ease-in-out infinite",
        auroraB: "auroraB 20s ease-in-out infinite",
        float: "float 6s ease-in-out infinite",
        shimmer: "shimmer 2.2s infinite",
        gradientmove: "gradientmove 6s ease infinite",
        "pulse-glow": "pulse-glow 2.4s ease-in-out infinite",
        "fade-up": "fade-up 0.6s cubic-bezier(0.22,1,0.36,1) both",
        marquee: "marquee 28s linear infinite",
        spinslow: "spinslow 8s linear infinite",
      },
    },
  },
  plugins: [],
};
