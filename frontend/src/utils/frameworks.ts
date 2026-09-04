/** Which language each test framework can actually be generated in.
 *
 *  ONE source of truth, because there were two and they disagreed with the
 *  backend and with each other:
 *
 *    • ConfigureStep offered Selenium + JavaScript. The writer has no such
 *      combination — `_normalize_framework` maps any non-Java Selenium request
 *      to `selenium_python`, so choosing JavaScript silently produced a Python
 *      suite.
 *    • Settings offered every framework against every language with no coupling
 *      at all, so "Cypress + Java" and "Selenium + TypeScript" could be saved as
 *      a user's defaults. Neither exists.
 *
 *  The backend's real set is the five template keys in scaffold/validator:
 *  playwright_js, playwright_python, cypress_js, selenium_java, selenium_python.
 *  This table is those five, expressed as the choices a user is allowed to make.
 *  Keep it in step with `WriterAgent._normalize_framework`.
 */
import type { Framework, Language } from "../types";

export const FRAMEWORKS: { id: Framework; label: string; desc: string }[] = [
  { id: "playwright", label: "Playwright", desc: "Fast, reliable, supports all browsers" },
  { id: "cypress", label: "Cypress", desc: "Great DX, real-time browser preview" },
  { id: "selenium", label: "Selenium", desc: "Industry standard, broad language support" },
];

export const LANGUAGES_FOR: Record<Framework, { id: Language; label: string }[]> = {
  // playwright_js covers both TS and JS; playwright_python is its own template.
  playwright: [
    { id: "typescript", label: "TypeScript" },
    { id: "javascript", label: "JavaScript" },
    { id: "python", label: "Python" },
  ],
  // cypress_js only. Cypress itself is JS/TS, so there is nothing else to offer.
  cypress: [
    { id: "typescript", label: "TypeScript" },
    { id: "javascript", label: "JavaScript" },
  ],
  // selenium_python and selenium_java only — deliberately no JavaScript.
  selenium: [
    { id: "python", label: "Python" },
    { id: "java", label: "Java" },
  ],
};

/** The language to use when `language` isn't valid for `framework`.
 *  Returns the current one when it is already fine, so callers can assign
 *  unconditionally. */
export function coerceLanguage(framework: Framework, language: Language): Language {
  const allowed = LANGUAGES_FOR[framework];
  return allowed.some((l) => l.id === language) ? language : allowed[0].id;
}

/** Whether a saved pair is still generatable — a stored default can predate a
 *  change to this table, and silently generating the wrong language is exactly
 *  the bug this file exists to prevent. */
export function isValidCombo(framework: Framework, language: Language): boolean {
  return (LANGUAGES_FOR[framework] ?? []).some((l) => l.id === language);
}
