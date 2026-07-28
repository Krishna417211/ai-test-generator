"""
test_secrets_guard.py — Credentials must not reach a model or a git repo.

Both destinations are irreversible: a prompt is gone into a provider's logs, and
a commit survives a force-push in the reflog. So these tests are written from
the leak's point of view — each one names a file someone really does have in
their working tree when they upload it.

The false-positive direction matters just as much and is tested alongside: a
guard that eats `.env.example` and `src/apiKey.ts` gets switched off, and then
it protects nobody.
"""

import pytest

from services import secrets_guard
from services.file_extractor import FileExtractor


class TestSharedSpecification:
    """The corpus in secret_rules.json, run against this implementation.

    `cli/test/secrets.test.js` runs the very same corpus against the JavaScript
    one. That is what makes "the two agree" a checked fact rather than a hope:
    the cases live with the rules, so adding one covers both languages, and
    neither implementation can pass a case the other fails.

    The classes below cover what a corpus entry cannot express — the scan window,
    and how scrub() partitions its input.
    """

    def test_corpus_is_substantial(self):
        assert len(secrets_guard.RULES["self_test"]) >= 30

    @pytest.mark.parametrize(
        "case",
        secrets_guard.RULES["self_test"],
        ids=lambda c: f"{'block' if c['excluded'] else 'allow'}:{c['path']}",
    )
    def test_case(self, case):
        finding = secrets_guard.inspect(case["path"], case["content"])
        why = case.get("note") or ("must be excluded" if case["excluded"] else "must be kept")
        if case["excluded"]:
            assert finding is not None, f"{case['path']}: expected excluded, was kept — {why}"
            assert finding.reason, "an exclusion needs a reason the user can act on"
            assert finding.kind in ("path", "content")
        else:
            assert finding is None, (
                f"{case['path']}: expected kept, was excluded as "
                f"{finding.reason!r} — {why}"
            )


class TestPathRules:
    def test_dotenv_is_caught(self):
        """The one that matters most, and the one a naive lstrip("./") misses —
        it strips characters, so ".env" becomes "env" and sails through."""
        assert secrets_guard.inspect(".env", "SECRET=1") is not None
        assert secrets_guard.inspect("./.env", "SECRET=1") is not None
        assert secrets_guard.inspect("backend/.env", "SECRET=1") is not None
        assert secrets_guard.inspect(".env.local", "SECRET=1") is not None
        assert secrets_guard.inspect(".env.production", "SECRET=1") is not None

    def test_dotenv_templates_are_kept(self):
        """Placeholders by definition, and the file that documents the app's
        configuration. Dropping them is a bug, not caution."""
        for name in (".env.example", ".env.sample", ".env.template", ".env.dist"):
            assert secrets_guard.inspect(name, "SECRET=") is None, name

    def test_keys_and_certs(self):
        for name in ("server.pem", "app.key", "keystore.jks", "cert.p12"):
            assert secrets_guard.inspect(name, "x") is not None, name

    def test_ssh_and_cloud_credential_files(self):
        assert secrets_guard.inspect("deploy/id_rsa", "x") is not None
        assert secrets_guard.inspect("id_ed25519", "x") is not None
        assert secrets_guard.inspect(".aws/credentials", "x") is not None
        assert secrets_guard.inspect(".ssh/config", "x") is not None
        assert secrets_guard.inspect("gcp-service-account-key.json", "{}") is not None

    def test_public_key_is_not_a_secret(self):
        assert secrets_guard.inspect("id_rsa.pub", "ssh-rsa AAAA") is None

    def test_ordinary_source_is_untouched(self):
        for name in ("src/App.tsx", "package.json", "README.md",
                     "src/hooks/useApiKey.ts", "tests/keyboard.spec.ts"):
            assert secrets_guard.inspect(name, "export const x = 1;") is None, name


class TestContentRules:
    def test_issuer_prefixed_tokens(self):
        cases = [
            'const k = "AKIAIOSFODNN7EXAMPLE"',
            'token = "ghp_' + "a" * 36 + '"',
            'stripe("sk_live_' + "a" * 24 + '")',
            'key = "AIza' + "a" * 35 + '"',
        ]
        for content in cases:
            assert secrets_guard.inspect("src/config.ts", content) is not None, content

    def test_private_key_block(self):
        body = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
        assert secrets_guard.inspect("config/dev.js", body) is not None

    def test_database_url_with_password(self):
        content = 'DATABASE_URL = "postgres://admin:hunter2xyz@db.internal:5432/app"'
        assert secrets_guard.inspect("settings.py", content) is not None

    def test_placeholder_assignments_are_not_flagged(self):
        """Deliberately not matched. Generic `api_key = "..."` is overwhelmingly
        a placeholder; matching it would drop half a codebase and teach users to
        distrust the feature."""
        for content in (
            'const API_KEY = "your-api-key-here";',
            'password = os.environ["DB_PASSWORD"]',
            'apiKey: process.env.VITE_API_KEY,',
            'const secret = "";',
        ):
            assert secrets_guard.inspect("src/config.ts", content) is None, content

    def test_only_the_head_of_a_file_is_scanned(self):
        """A bounded scan is a deliberate trade: a real key sits in a config
        block near the top, and reading every byte of every vendor bundle on
        every request costs more than it catches."""
        big = "x" * 200_000 + " AKIAIOSFODNN7EXAMPLE"
        assert secrets_guard.inspect("vendor/bundle.js", big) is None


class TestScrubAndReport:
    def test_scrub_splits_and_reports(self):
        files = {"src/App.tsx": "code", ".env": "K=v", "id_rsa": "key"}
        safe, excluded = secrets_guard.scrub(files)
        assert safe == {"src/App.tsx": "code"}
        assert {f.path for f in excluded} == {".env", "id_rsa"}

    def test_summary_names_the_files(self):
        """Counting them leaves the user guessing which one we mean, and going
        to check is the entire value of the message."""
        _, excluded = secrets_guard.scrub({".env": "K=v"})
        msg = secrets_guard.summarize(excluded)
        assert ".env" in msg and "1 sensitive file" in msg

    def test_no_secrets_means_no_message(self):
        assert secrets_guard.summarize([]) == ""


class TestExtractorIntegration:
    """The guard has to run inside the pipeline, not just be importable."""

    def test_dotenv_never_reaches_the_llm_context(self):
        result = FileExtractor().extract({
            "src/App.tsx": "<div id='root'>hi</div>",
            ".env": "STRIPE_SECRET=sk_live_abc",
        })
        assert ".env" not in result.files
        assert any(".env" in w for w in result.warnings)
        assert result.excluded_secrets[0]["path"] == ".env"
