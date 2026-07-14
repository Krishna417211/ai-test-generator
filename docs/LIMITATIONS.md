# Testra — Model Card & Limitations

Testra uses LLMs to read a codebase and write E2E tests. LLM output is
probabilistic, so read this before relying on the results in CI.

## What "validated" means (and doesn't)

The green **"passed a syntax check"** badge on the results screen means each
generated file passed a **static/structural check only** (it parses / has the
expected shape). It does **not** mean:

- the tests were executed,
- the selectors resolve against your running app, or
- the assertions pass.

Always run the generated suite against your app before trusting it in CI.

## Known limitations

- **Selector hallucination.** The writer is grounded on selectors extracted from
  your source and flags suspected invented ones with `⚠️`, but the extraction is
  regex-based today — it can miss dynamic classes (`className={styles.x}`),
  CSS-in-JS, and ARIA/role/text selectors, so both false positives and false
  negatives are possible. Review selectors marked "not verified".
- **Large repos.** Very large repositories may exceed the model's context window;
  low-priority files are summarized rather than sent verbatim, which can reduce
  selector accuracy. (Retrieval-based context selection is on the roadmap.)
- **Output size.** Non-trivial suites are emitted as structured output; extremely
  large suites can hit provider output-token limits. If a suite looks truncated,
  narrow the "what to test" scope and regenerate.
- **Free-tier providers.** Generation quality/latency depends on whichever free
  provider is available (Gemini → Groq → Claude). Under rate limits it
  falls back automatically, so results can vary run to run.

## Data handling

- Repo contents are held only for the duration of a job (SQLite, pruned after
  24h) and sent to the selected LLM provider to generate tests.
- Linked GitHub access tokens are encrypted at rest when `SESSION_SECRET` is set.
- The security scanner is passive and refuses internal/private targets (SSRF
  guard, including redirect hops).

## Roadmap toward higher confidence

- Execute generated suites in a sandbox and report real pass/fail (not just syntax).
- AST-based selector extraction (roles, accessible names, visible text).
- Retrieval + per-provider context budgeting for large repos.
