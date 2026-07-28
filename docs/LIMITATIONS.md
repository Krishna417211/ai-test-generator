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

## What the success rate means (and doesn't)

The **generation success rate** on the results screen is the share of our
automated checks the suite passed: planned files delivered, code files parsed,
selectors traced back to your app, and test cases produced. It is weighted, and a
check with nothing to measure is dropped rather than scored as a free 100%.

It is **not a pass rate.** A suite can score 100 and still fail on its first run
— every file was written, all of it parses, every selector exists somewhere in
your app, and the assertion about what the page *does* after clicking is still
the model's guess. We never execute the tests, so a number labelled "accuracy"
would be invented. Expand "See the N checks" to see exactly which measurements
produced the score.

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
  falls back automatically, so results can vary run to run. The free tiers also
  meter tokens *per day*, not just per minute: a large suite can exhaust a day's
  budget in one run, and every provider being dry is a plain 503 (never an
  upgrade prompt — see `services/quota.py`), because paying wouldn't fix it.
- **What the crawl can reach is what the suite can test.** The generate flow
  writes tests from the live site, so anything the crawler can't see, it can't
  test. Two cases bite:
  - *Behind a sign-in.* Tick **"This site needs a login"** and the crawl signs in
    first, which is usually the difference between grounding on one form and
    grounding on the app (measured on a demo store: 21 anchors → 129, and
    checkout/confirmation reachable at all). Without it, a guarded app shows the
    crawler nothing but its login screen. Wrong credentials fail the run loudly
    rather than quietly crawling the login page.
  - *Behind an interaction.* A modal, a wizard step or a menu that only opens on
    click is not visited — the crawl follows links and app routes, it does not
    drive the UI. Selectors that only exist in those states won't be grounded.

## What we refuse to send or write

- **Credential files never leave the process.** `.env` (but not `.env.example`),
  SSH keys, `.pem`/`.key`/`.p12`/`.jks`, `.npmrc`, `.netrc`, `terraform.tfstate`,
  anything under `.aws/` `.ssh/` `.gnupg/`, cloud service-account JSONs, and any
  file containing an issuer-prefixed token (`AKIA…`, `ghp_…`, `sk_live_…`) or a
  PEM private-key block. They are withheld from the LLM prompt *and* from the
  push, and every one is named in the response — a `.env` that silently vanished
  would leave you debugging a missing-config error we caused. See
  `backend/services/secrets_guard.py`.
- **Nothing is written over your own files.** Generated paths are sanitized
  (no traversal, no absolute paths, no `.git/`, no Windows-hostile names) and
  merged without overwriting anything already in your project. A collision keeps
  your version and says so. See `backend/services/safe_paths.py`.
- **"N files pushed" is checked, not assumed.** After the push the commit tree is
  read back and every expected file confirmed present with the exact content hash
  we uploaded. Missing files are re-pushed once; anything still missing fails the
  publish rather than reporting success.

## Data handling

- Repo contents are held only for the duration of a job (SQLite, pruned after
  24h) and sent to the selected LLM provider to generate tests — minus the
  credential files described above, which are never sent.
- Linked GitHub access tokens are encrypted at rest when `SESSION_SECRET` is set.
- The security scanner is passive and refuses internal/private targets (SSRF
  guard, including redirect hops).

## Roadmap toward higher confidence

- Execute generated suites in a sandbox and report real pass/fail (not just syntax).
- AST-based selector extraction (roles, accessible names, visible text).
- Retrieval + per-provider context budgeting for large repos.
