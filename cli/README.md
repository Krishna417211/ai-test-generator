# testra (CLI)

> Publish a project to a fresh GitHub repo, with a generated E2E suite, from the directory it lives in.

```bash
npx @testra/cli login
npx @testra/cli publish --with-ci
```

No install, no dependencies — the package has **zero** runtime dependencies and
uses only Node's standard library.

> **Not on npm yet.** The commands above are what will work once it is published.
> Until then, run it from a clone:
>
> ```bash
> git clone https://github.com/Krishna417211/ai-test-generator
> node ai-test-generator/cli/bin/testra.js login
> node ai-test-generator/cli/bin/testra.js publish --with-ci
> ```
>
> Note that the unscoped name `testra` on npm belongs to an unrelated project (a
> minimal test runner), which is why this package is scoped. Do not run
> `npx testra` — it is somebody else's code.

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
testra login                       Approve a code in a browser (--token <t> for CI)
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

`testra login` uses the **device-code flow** ([RFC 8628](https://datatracker.ietf.org/doc/html/rfc8628)):

```
$ testra login

  Your code:  BDFG-HJKM
  Approve at: https://app.testra.dev/cli?code=BDFG-HJKM

  Waiting for approval… (Ctrl-C to cancel)

✓ Signed in as dev@example.com
```

**Your password never passes through this tool.** You approve the code in a
browser — and it does not have to be a browser on this machine, so this works
over SSH on a box with no browser at all. A phone is fine.

Two codes are involved and they are not the same kind of thing. The short one
(`BDFG-HJKM`) is designed to be read off one screen and typed into another, so it
is **not** a secret — it grants nothing until someone already signed in approves
it, it dies after 10 minutes, and submissions are hard rate-limited. The other,
which the CLI holds and never prints, is the secret that entitles it to collect
the session token — exactly once.

If a code you didn't ask for appears in your browser, press **Reject**.

### Other ways in

Resolution order, most explicit first:

1. **`TESTRA_TOKEN`** in the environment — use this in CI, from a secret store
2. **`~/.config/testra/config.json`** — written by `testra login`, mode `0600` in
   a `0700` directory (`XDG_CONFIG_HOME` is honoured if set)

`testra login --email you@example.com` falls back to a password prompt (echo
suppressed, never stored). It exists for a machine that can reach no browser at
all, not even a phone — prefer the device flow.

`testra logout` removes the local copy only; the session stays valid until it
expires. Use *log out everywhere* in the web app to revoke it server-side.

Publishing pushes to GitHub on your behalf, so the **account needs a linked
GitHub identity** — connect it once in the web app. The CLI never handles your
GitHub token; the server holds it, encrypted at rest.

### In CI

```yaml
- run: npx @testra/cli publish --repo ${{ github.event.repository.name }}-e2e --with-ci --yes
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
npm test
```

The ZIP writer is hand-rolled on top of `node:zlib` so the package can have no
dependencies. That is only defensible because it is verified against its real
consumer: `backend/tests/test_cli_zip.py` builds an archive with it and extracts
it with the server's own `extract_zip()`, the exact function `/api/publish-zip`
calls.

### The credential rules are shared, not duplicated

`src/secret-rules.json` is a verbatim copy of
`backend/services/secret_rules.json`, the canonical set that the Python guard
reads too. The copy exists only because npm publishes `cli/` alone and a package
cannot read a file outside its own directory.

```bash
npm run sync-rules        # refresh the copy from the canonical file
```

**Never hand-edit the copy.** Edit the canonical file and re-run that —
`backend/tests/test_secret_rules_sync.py` compares the two byte for byte and
fails CI on any difference, naming the command.

The `self_test` array inside that JSON is the *specification* of what the rules
decide, and both languages execute it (`cli/test/secrets.test.js` and
`backend/tests/test_secrets_guard.py`). Add a case there and both are covered at
once — neither implementation can pass a case the other fails.

Patterns must stay inside the subset that means the same thing in Python `re` and
JavaScript `RegExp`. `test_secret_rules_sync.py` rejects named groups, lookbehind
and other one-engine constructs.


---

## Publishing

```bash
cd cli
npm publish --access public
```

The scope makes `--access public` necessary: npm treats scoped packages as
private by default, and `publishConfig.access` in package.json sets it too so a
bare `npm publish` cannot accidentally publish a private one.

There is nothing to build — `files` in package.json ships `bin/`, `src/` and this
README, and there are no dependencies to resolve. Check what would go in the
tarball first:

```bash
npm pack --dry-run
```

**Why the package is scoped.** The unscoped name `testra` was already taken on
npm by an unrelated "minimal test runner". Documenting `npx testra` would have
told every user to download and execute a stranger's code — on a tool whose
entire premise is that your credentials never leave your machine. A scope also
makes the name unsquattable once the org is owned.
