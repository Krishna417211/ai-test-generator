"""
test_security_hardening.py — Four defences, each written from the attack.

Every case here starts from something someone would actually try, because a
security test that only asserts the happy path proves nothing about the case it
was written for.

  1. Session tokens at rest — a read-only glimpse of the database must not be
     usable as a login.
  2. Per-account login throttling — a credential-stuffing run spread across many
     addresses must be capped by the *account*, not just the caller.
  3. Zip bombs — a compliant 10 MB upload must not be able to expand to gigabytes
     and take the process out.
  4. Security headers — a directly-exposed API (dev, or a deploy without the
     proxy) must not be bare.
"""

import io
import os
import sqlite3
import tempfile
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

import main
import ratelimit
from services import auth as auth_svc
from services import login_guard
from services import file_extractor
from services.file_extractor import extract_zip, ZipBombError
from services.store import JobStore


@pytest.fixture(autouse=True)
def _fresh_limiters():
    for name in dir(ratelimit):
        limiter = getattr(ratelimit, name)
        if isinstance(limiter, ratelimit.SlidingWindowLimiter):
            limiter._hits.clear()
    yield


@pytest.fixture
def client(monkeypatch):
    store = JobStore(tempfile.mktemp(suffix=".db"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(auth_svc, "store", store)
    monkeypatch.setattr(login_guard, "store", store)
    c = TestClient(main.app)
    c.store = store
    return c


# ── 1. session tokens at rest ────────────────

class TestSessionTokensAtRest:
    """A bearer token is the whole credential. Stored verbatim, one look at the
    database — a leaked backup, a volume snapshot, an injection anywhere — is
    live takeover of every signed-in account at once."""

    def _db_keys(self, store) -> list[str]:
        with sqlite3.connect(store.db_path if hasattr(store, "db_path") else store._path) as c:
            return [r[0] for r in c.execute("SELECT session_id FROM sessions")]

    def test_the_token_itself_is_never_in_the_database(self, client):
        client.store.create_user({"id": "u1", "email": "a@b.c", "password_hash": "x",
                                  "email_verified": 1})
        token = auth_svc.create_login_session("u1")

        stored = self._db_keys(client.store)
        assert stored, "a session row should exist"
        assert token not in stored, "the raw token must not be stored"
        assert all(len(k) == 64 for k in stored), "keys should be sha256 hex digests"

    def test_the_session_still_works_through_the_api(self, client):
        client.store.create_user({"id": "u1", "email": "a@b.c", "password_hash": "x",
                                  "email_verified": 1})
        token = auth_svc.create_login_session("u1")
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text

    def test_a_stolen_database_key_is_not_a_login(self, client):
        """The point of the whole change: presenting what's in the table must
        fail, or hashing has bought nothing."""
        client.store.create_user({"id": "u1", "email": "a@b.c", "password_hash": "x",
                                  "email_verified": 1})
        auth_svc.create_login_session("u1")
        leaked = self._db_keys(client.store)[0]

        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {leaked}"})
        assert r.status_code == 401

    def test_oauth_state_and_cli_codes_are_covered_too(self, client):
        """They share the table, so they share the protection — a leaked CSRF
        state or device code is as dangerous as a session."""
        body = client.post("/api/auth/cli/start").json()
        stored = self._db_keys(client.store)
        assert body["device_code"] not in stored
        assert body["user_code"] not in stored

    def test_logout_everywhere_still_finds_the_sessions(self, client):
        """Revocation matches on the stored hash and spares the caller's own
        token — the one place the plaintext has to be hashed to compare."""
        client.store.create_user({"id": "u1", "email": "a@b.c", "password_hash": "x",
                                  "email_verified": 1})
        keep = auth_svc.create_login_session("u1")
        other = auth_svc.create_login_session("u1")

        removed = client.store.delete_sessions_for_user("u1", keep=keep)
        assert removed == 1
        assert client.store.get_session(keep) is not None, "the caller keeps their session"
        assert client.store.get_session(other) is None


class TestLegacyMigration:
    """Rows written before hashing hold the token. Hashing them in place is what
    a lookup now computes, so nobody is logged out — but running it twice would
    log out everyone with no way back, hence the PRAGMA guard."""

    def _legacy_db(self) -> str:
        path = tempfile.mktemp(suffix=".db")
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, "
                  "created_at REAL NOT NULL, expires_at REAL NOT NULL, data TEXT NOT NULL)")
        c.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                  ("legacy_tok", time.time(), time.time() + 9999, '{"user_id":"u1"}'))
        c.commit()
        c.close()
        return path

    def test_existing_sessions_survive(self):
        store = JobStore(self._legacy_db())
        assert store.get_session("legacy_tok")["user_id"] == "u1"

    def test_migration_is_idempotent(self):
        path = self._legacy_db()
        JobStore(path)
        again = JobStore(path)          # a restart must not re-hash
        assert again.get_session("legacy_tok") is not None


# ── 2. per-account login throttling ──────────

class TestLoginThrottling:
    def _account(self, client, email="victim@example.com", password="correct-horse"):
        client.store.create_user({
            "id": "u1", "email": email,
            "password_hash": auth_svc.hash_password(password),
            "email_verified": 1,
        })
        return email, password

    def test_repeated_wrong_passwords_eventually_throttle(self, client):
        email, _ = self._account(client)
        codes = [
            client.post("/api/auth/login", json={"email": email, "password": "wrong"}).status_code
            for _ in range(8)
        ]
        assert codes[0] == 401, "the first attempt is just wrong, not throttled"
        assert 429 in codes, "sustained guessing must be throttled"

    def test_a_few_typos_cost_nothing(self, client):
        """The overwhelmingly common case. A throttle that fires on the second
        typo is one users route around by resetting their password."""
        email, password = self._account(client)
        for _ in range(3):
            client.post("/api/auth/login", json={"email": email, "password": "typo"})
        r = client.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code != 429, "three failures must not lock a correct password out"

    def test_the_correct_password_clears_the_counter(self, client):
        email, password = self._account(client)
        for _ in range(3):
            client.post("/api/auth/login", json={"email": email, "password": "typo"})
        client.post("/api/auth/login", json={"email": email, "password": password})
        assert login_guard.check(email) == (True, 0)

    def test_it_never_disables_the_account(self, client):
        """Locking an account outright hands anyone who knows an email address a
        denial-of-service button. The delay must expire on its own."""
        email, password = self._account(client)
        for _ in range(8):
            client.post("/api/auth/login", json={"email": email, "password": "wrong"})

        # Fast-forward past the backoff rather than sleeping through it.
        record = client.store.get_session("loginfail:" + email)
        record["retry_at"] = time.time() - 1
        client.store.create_session("loginfail:" + email, record, 3600)

        r = client.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code != 429, "the account must recover on its own"

    def test_the_throttle_is_not_an_account_existence_oracle(self, client):
        """If only real accounts slowed down, the throttle would answer the
        question the 401 message carefully refuses to."""
        self._account(client)
        real = [client.post("/api/auth/login",
                            json={"email": "victim@example.com", "password": "x"}).status_code
                for _ in range(8)]
        ratelimit.analyze_limiter._hits.clear()
        fake = [client.post("/api/auth/login",
                            json={"email": "nobody@example.com", "password": "x"}).status_code
                for _ in range(8)]
        assert (429 in real) == (429 in fake)

    def test_backoff_grows_and_then_plateaus(self):
        curve = [login_guard.backoff_for(n) for n in range(1, 10)]
        assert curve[0] == 0, "the first failure is free"
        assert curve == sorted(curve), "delay must never decrease"
        assert curve[-1] == curve[-2], "it plateaus rather than growing forever"


# ── 3. zip bombs ─────────────────────────────

def _zip_of(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestZipBombs:
    def test_an_ordinary_project_is_unaffected(self):
        archive = _zip_of({
            "src/App.tsx": b"<div/>" * 100,
            "package.json": b'{"name":"demo"}',
        })
        assert set(extract_zip(archive)) == {"src/App.tsx", "package.json"}

    def test_a_single_huge_member_is_refused(self):
        """The classic bomb: one member of highly repetitive data. 60 MB of
        zeroes compresses to a few KB — well inside any upload cap."""
        archive = _zip_of({"bomb.txt": b"\0" * (60 * 1024 * 1024)})
        assert len(archive) < 1024 * 1024, "the bomb should be small on the wire"
        with pytest.raises(ZipBombError, match="per-file limit"):
            extract_zip(archive)

    def test_many_medium_members_are_refused_in_aggregate(self, monkeypatch):
        """The rule the ratio check cannot cover: members that are each entirely
        reasonable, and ruinous together.

        The members must be INCOMPRESSIBLE or the ratio rule fires first and this
        silently stops testing aggregation — which is exactly what the first
        version of this test did, using `b"a" * 20MB`. Random bytes give a ratio
        of ~1, so only the total can reject them.

        The ceiling is lowered rather than allocating 400 MB in a unit test; the
        real constants are checked separately below.
        """
        monkeypatch.setattr(file_extractor, "MAX_TOTAL_UNCOMPRESSED", 4 * 1024 * 1024)
        member = os.urandom(1024 * 1024)          # 1 MB, effectively incompressible
        archive = _zip_of({f"f{i}.txt": member for i in range(6)})

        with pytest.raises(ZipBombError, match="expands to"):
            extract_zip(archive)

    def test_the_running_total_catches_an_archive_that_lied(self, monkeypatch):
        """Header sizes are attacker-controlled, so the header check alone is not
        a defence — this is the one that counts what actually came out."""
        member = os.urandom(1024 * 1024)
        archive = _zip_of({f"f{i}.txt": member for i in range(6)})

        # Pass the header pass, then trip the extraction-time total.
        monkeypatch.setattr(file_extractor, "_reject_zip_bombs", lambda zf: None)
        monkeypatch.setattr(file_extractor, "MAX_TOTAL_UNCOMPRESSED", 3 * 1024 * 1024)

        with pytest.raises(ZipBombError, match="understated"):
            extract_zip(archive)

    def test_the_shipped_limits_are_sane(self):
        """Guards the constants themselves, since the tests above move them."""
        assert file_extractor.MAX_SINGLE_UNCOMPRESSED < file_extractor.MAX_TOTAL_UNCOMPRESSED
        # Comfortably above any real source file, well below what would OOM.
        assert 10 * 1024 * 1024 <= file_extractor.MAX_SINGLE_UNCOMPRESSED <= 200 * 1024 * 1024
        assert file_extractor.MAX_COMPRESSION_RATIO >= 50, "too tight would reject real archives"

    def test_a_high_ratio_member_is_refused(self):
        archive = _zip_of({"ratio.txt": b"A" * (5 * 1024 * 1024)})
        with pytest.raises(ZipBombError):
            extract_zip(archive)

    def test_small_files_are_not_judged_on_ratio(self):
        """A 12-byte file compressing to 1 byte is a 12:1 ratio and completely
        ordinary — ratios below the floor mean nothing."""
        archive = _zip_of({"tiny.txt": b"aaaaaaaaaaaa", "also.txt": b"bb"})
        assert len(extract_zip(archive)) == 2

    def test_the_endpoint_answers_413_rather_than_crashing(self, client):
        client.store.create_user({"id": "u1", "email": "a@b.c", "password_hash": "x",
                                  "email_verified": 1})
        token = auth_svc.create_login_session("u1")
        archive = _zip_of({"bomb.txt": b"\0" * (60 * 1024 * 1024)})

        r = client.post(
            "/api/upload-zip",
            files={"file": ("p.zip", archive, "application/zip")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 413
        assert "bomb.txt" in r.json()["detail"], "the message should name the file"


# ── 4. security headers ──────────────────────

class TestSecurityHeaders:
    def test_api_responses_carry_the_baseline_headers(self, client):
        r = client.get("/health")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]

    def test_error_responses_carry_them_too(self, client):
        """An error body is exactly what you'd want re-interpreted as HTML, so
        nosniff must survive the failure path."""
        r = client.get("/api/auth/me")
        assert r.status_code == 401
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_the_docs_page_is_left_alone(self, client):
        """`default-src 'none'` would render Swagger blank — it loads its bundle
        from a CDN and sets no policy of its own."""
        r = client.get("/docs")
        if r.status_code == 404:
            pytest.skip("docs disabled in this configuration")
        assert "Content-Security-Policy" not in r.headers


# ── 5. profile pictures ──────────────────────

class TestAvatar:
    """A picture is user-supplied content that every page renders, so the
    interesting cases are what must be refused."""

    def _png(self, n=40):
        import base64
        return "data:image/png;base64," + base64.b64encode(b"x" * n).decode()

    def _auth(self, client):
        client.store.create_user({"id": "u1", "email": "a@b.c", "password_hash": "x",
                                  "email_verified": 1})
        return {"Authorization": f"Bearer {auth_svc.create_login_session('u1')}"}

    def test_a_png_is_accepted_and_stored(self, client):
        h = self._auth(client)
        r = client.put("/api/profile/avatar", json={"image": self._png()}, headers=h)
        assert r.status_code == 200, r.text
        assert client.store.get_user_by_id("u1")["avatar_url"].startswith("data:image/png")

    def test_svg_is_refused(self, client):
        """The one that matters. An SVG is a document that can carry <script>;
        browsers mostly refuse to run it inside an <img>, but that is their
        behaviour to change, not ours to rely on."""
        import base64
        svg = "data:image/svg+xml;base64," + base64.b64encode(
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        ).decode()
        r = client.put("/api/profile/avatar", json={"image": svg}, headers=self._auth(client))
        assert r.status_code == 422
        assert "SVG" in r.text

    def test_a_remote_url_is_refused(self, client):
        """Only data: URIs. A remote URL would be fetched by every viewer's
        browser, turning a profile picture into a tracking pixel."""
        r = client.put("/api/profile/avatar",
                       json={"image": "https://evil.example/track.png"},
                       headers=self._auth(client))
        assert r.status_code == 422

    def test_an_oversized_picture_is_refused(self, client):
        import base64
        big = "data:image/png;base64," + base64.b64encode(b"x" * (300 * 1024)).decode()
        r = client.put("/api/profile/avatar", json={"image": big},
                       headers=self._auth(client))
        assert r.status_code == 422
        assert "too large" in r.text

    def test_malformed_base64_is_refused(self, client):
        r = client.put("/api/profile/avatar",
                       json={"image": "data:image/png;base64,!!!not-base64!!!"},
                       headers=self._auth(client))
        assert r.status_code == 422

    def test_empty_clears_the_picture(self, client):
        h = self._auth(client)
        client.put("/api/profile/avatar", json={"image": self._png()}, headers=h)
        r = client.put("/api/profile/avatar", json={"image": ""}, headers=h)
        assert r.status_code == 200
        assert not client.store.get_user_by_id("u1")["avatar_url"]
        assert "initial" in r.json()["message"]

    def test_it_requires_a_session(self, client):
        r = client.put("/api/profile/avatar", json={"image": self._png()})
        assert r.status_code in (401, 403)
