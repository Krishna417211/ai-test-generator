# testra (CLI)

> Publish a project to a fresh GitHub repo, with a generated E2E suite, from the directory it lives in.

```bash
npx testra login
npx testra publish --with-ci
```

No install, no dependencies — the package has **zero** runtime dependencies and
uses only Node's standard library.

---

## Why a CLI and not the web app

The web app has to receive a ZIP that you made by hand. This runs *inside* your
project, which buys three things the upload page cannot offer:

**Your `.gitignore` decides what is uploaded.** Files come from
`git ls-files -co --exclude-standard` — git's own answer to "what belongs to this
project", honouring nested `.gitignore`s, `.git/info/exclude` and your global
excludes. The server has to guess with a fixed skip-list, which knows about
`node_modules` and `dist` but not `.turbo`, `.astro`, or whatever this year's
build tool named its cache. Yours already lists them.

**Credentials never leave your machine.** The scan for `.env` files, SSH keys,
`.pem`s and hard-coded API tokens runs *before* the upload, not after it. On the
web the server is the earliest possible place to catch a `.env`; by then it has
already crossed the network. Here it never does. (The server re-scans anyway —
defence in depth — so a rule the CLI lacks is still caught. Drift can only ever
mean "sent something it could have stopped locally", never "a credential got
through".)

**It runs in CI.** Set `TESTRA_TOKEN` from a secret and it works headlessly.

---

## Commands

```
testra publish [dir] [options]     Create a GitHub repo and push this project
testra login [--email <e>]         Sign in (--token <t> for CI)
testra logout                      Remove the locally stored token
testra whoami                      Show the signed-in account
```

### `publish`

| Option | Default | |
|---|---|---|
| `--repo <name>` | directory name | Repository name |
| `--public` | private | Create it public |
| `--with-ci` | off | Also generate an E2E suite + CI/CD pipeline |
| `--framework <f>` | `playwright` | `playwright` \| `cypress` \| `selenium` |
| `--language <l>` | `typescript` | `typescript` \| `javascript` \| `python` \| `java` |
| `--flows <text>` | — | What to test, in prose |
| `--base-url <url>` | `http://localhost:3000` | Where the app under test listens |
| `--description <text>` | — | Repository description |
| `-y, --yes` | off | Skip the confirmation prompt |

Private is the default deliberately: a repo made public by accident cannot be
made un-public after someone has cloned it.

```bash
# Everything, non-interactively
testra publish --repo checkout-service --with-ci \
  --framework playwright --language typescript \
  --flows "sign in, add to cart, complete checkout" \
  --base-url https://staging.example.com --yes
```

---

## Authentication

Resolution order, most explicit first:

1. **`TESTRA_TOKEN`** in the environment — use this in CI, from a secret store
2. **`~/.config/testra/config.json`** — written by `testra login`, mode `0600` in
   a `0700` directory (`XDG_CONFIG_HOME` is honoured if set)

`testra logout` removes the local copy only; the session stays valid until it
expires. Use *log out everywhere* in the web app to revoke it server-side.

Publishing pushes to GitHub on your behalf, so the **account needs a linked
GitHub identity** — connect it once in the web app. The CLI never handles your
GitHub token; the server holds it, encrypted at rest.

### In CI

```yaml
- run: npx testra publish --repo ${{ github.event.repository.name }}-e2e --with-ci --yes
  env:
    TESTRA_TOKEN: ${{ secrets.TESTRA_TOKEN }}
```

---

## Environment

| Variable | |
|---|---|
| `TESTRA_TOKEN` | Session token; overrides the stored one |
| `TESTRA_API_URL` | API base URL (default `http://localhost:8000`) |
| `NO_COLOR` | Disable colour (also auto-disabled when stdout isn't a TTY) |

---

## Exit codes

| | |
|---|---|
| `0` | Success |
| `1` | Failure — the message says what and, where possible, what to do |
| `130` | Cancelled at a prompt |

---

## Development

```bash
node --test "test/**/*.test.js"
```

The ZIP writer is hand-rolled on top of `node:zlib` so the package can have no
dependencies. That is only defensible because it is verified against its real
consumer: `backend/tests/test_cli_zip.py` builds an archive with it and extracts
it with the server's own `extract_zip()`, the exact function `/api/publish-zip`
calls.

`src/secrets.js` mirrors `backend/services/secrets_guard.py`. **If you add a rule
to one, add it to the other and to both test suites** — the two suites are
written case-for-case so a missing one is visible.
