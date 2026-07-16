"""
store.py — Durable job/session store backed by SQLite.

Replaces the old in-memory `_sessions` dict so jobs survive server restarts
and are shared across worker processes. Uses only the stdlib `sqlite3`
(no external service needed). Each job holds the extracted files, the
FilterResult from Agent 1, the original request, and any streamed output.
"""

import os
import json
import time
import uuid
import sqlite3
import logging
from threading import Lock
from datetime import date, timedelta
from dataclasses import asdict

from agents.filter_agent import FilterResult

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testgen.db"
)

# Jobs older than this are pruned on startup (24h).
JOB_TTL_SECONDS = int(os.getenv("TESTGEN_JOB_TTL", str(24 * 60 * 60)))

# The three activity tables, and the feature each one records. The dashboard
# reads them as one timeline, so they're listed once here rather than spelled
# out at each of the three call sites that fan out over them.
ACTIVITY_TABLES = (
    ("generate", "generations"),
    ("publish", "published_repos"),
    ("scan", "scans"),
)

# Columns added after these tables shipped. CREATE TABLE IF NOT EXISTS skips a
# table that already exists, so a deployed database only gains them by ALTER.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "users": (
        ("plan", "plan TEXT NOT NULL DEFAULT 'free'"),
        # Epoch seconds; NULL means the plan doesn't expire. A lapsed
        # plan_expires_at reads as 'free' without a write (see get_plan).
        ("plan_expires_at", "plan_expires_at REAL"),
        # Defaults to 1, not 0: accounts that predate email verification were
        # created when the address was never checked, and flipping them to
        # unverified would lock out every existing user at once. New rows pass
        # email_verified=0 explicitly (see main.signup).
        ("email_verified", "email_verified INTEGER NOT NULL DEFAULT 1"),
        # Admin-applied lock. Separate from deletion because it's reversible and
        # keeps the audit trail: a suspended account's generations and scans stay
        # queryable, which is usually the whole reason it was suspended.
        ("suspended", "suspended INTEGER NOT NULL DEFAULT 0"),
        ("suspended_at", "suspended_at REAL"),
        ("suspended_reason", "suspended_reason TEXT"),
        # Stripe's customer id (cus_...). Only a checkout session echoes back the
        # client_reference_id we tagged it with; the invoice.paid that arrives on
        # every later renewal carries the customer and nothing else. Without this
        # mapping a renewal cannot be attributed and every subscriber would lapse
        # at the end of their first period.
        ("stripe_customer_id", "stripe_customer_id TEXT"),
    ),
    "generations": (
        # Defaults to 'success' because that's what every existing row is: until
        # now a generation was only recorded after it had already succeeded.
        ("status", "status TEXT NOT NULL DEFAULT 'success'"),
        ("duration_ms", "duration_ms INTEGER"),
        ("error", "error TEXT"),
    ),
    "published_repos": (
        ("test_count", "test_count INTEGER NOT NULL DEFAULT 0"),
        ("files_pushed", "files_pushed INTEGER NOT NULL DEFAULT 0"),
        ("cicd_added", "cicd_added INTEGER NOT NULL DEFAULT 0"),
        ("private", "private INTEGER NOT NULL DEFAULT 1"),
    ),
    "scans": (
        ("duration_ms", "duration_ms INTEGER"),
        # JSON severity breakdown ({"high": 2, ...}) — a blob, never filtered on.
        ("counts", "counts TEXT"),
    ),
}


class JobStore:
    """Thread-safe SQLite-backed store for analysis/generation jobs."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.getenv("TESTGEN_DB", _DEFAULT_DB)
        self._lock = Lock()
        self._init_schema()
        self.prune()

    def _conn(self) -> sqlite3.Connection:
        # check_same_thread=False + our own lock keeps this safe under asyncio.
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init_schema(self):
        with self._lock, self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id     TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    data       TEXT NOT NULL
                )
                """
            )
            # Login sessions: session_id → {user_id, github_token, ...} with expiry.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    data       TEXT NOT NULL
                )
                """
            )
            # User accounts (email/password and/or GitHub-linked).
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            TEXT PRIMARY KEY,
                    email         TEXT UNIQUE,
                    password_hash TEXT,
                    name          TEXT,
                    github_id     TEXT,
                    github_login  TEXT,
                    avatar_url    TEXT,
                    created_at    REAL NOT NULL
                )
                """
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_users_github ON users(github_id)")
            # Metered generation usage, one row per user per calendar month.
            # Counting rows would need the jobs table to live forever; a counter
            # survives job pruning.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    user_id TEXT NOT NULL,
                    period  TEXT NOT NULL,
                    count   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, period)
                )
                """
            )
            # Repos this app created, per user. Deletion is only ever offered
            # for rows in here — never for arbitrary repos the token can reach.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS published_repos (
                    full_name    TEXT NOT NULL,
                    user_id      TEXT NOT NULL,
                    repo_url     TEXT NOT NULL,
                    created_at   REAL NOT NULL,
                    test_count   INTEGER NOT NULL DEFAULT 0,
                    files_pushed INTEGER NOT NULL DEFAULT 0,
                    cicd_added   INTEGER NOT NULL DEFAULT 0,
                    private      INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (full_name, user_id)
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_repos_user "
                "ON published_repos(user_id, created_at DESC)"
            )
            # Completed work, kept for the user's own history. Deliberately
            # separate from `jobs`, which holds whole file bodies and is pruned
            # at 24h — these rows are small and are never pruned, so a profile
            # can still show what someone did months ago. `usage` can't serve
            # this: it's a bare per-month counter with no detail.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS generations (
                    id          TEXT PRIMARY KEY,
                    user_id     TEXT NOT NULL,
                    created_at  REAL NOT NULL,
                    source      TEXT,
                    framework   TEXT,
                    language    TEXT,
                    test_count  INTEGER NOT NULL DEFAULT 0,
                    file_count  INTEGER NOT NULL DEFAULT 0,
                    status      TEXT NOT NULL DEFAULT 'success',
                    duration_ms INTEGER,
                    error       TEXT
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_generations_user "
                "ON generations(user_id, created_at DESC)"
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    id          TEXT PRIMARY KEY,
                    user_id     TEXT NOT NULL,
                    created_at  REAL NOT NULL,
                    url         TEXT NOT NULL,
                    grade       TEXT,
                    score       INTEGER,
                    findings    INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER,
                    counts      TEXT
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_scans_user "
                "ON scans(user_id, created_at DESC)"
            )
            # Per-user defaults for the Generate/Publish forms. Absent row means
            # "never customised" — read_settings fills in DEFAULT_SETTINGS rather
            # than writing a row for every user who never opens Settings.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id    TEXT PRIMARY KEY,
                    framework  TEXT,
                    language   TEXT,
                    base_url   TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._migrate(c)
            # After _migrate, not beside the users table above: stripe_customer_id
            # arrives by ALTER, so on a deployed database the column does not exist
            # until _migrate has run.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_stripe_customer "
                "ON users(stripe_customer_id)"
            )
        logger.info(f"JobStore ready at {self.db_path}")

    def _migrate(self, c: sqlite3.Connection) -> None:
        """Add columns to tables that already exist in deployed databases.

        CREATE TABLE IF NOT EXISTS silently skips a table that's already there,
        so new columns need an explicit ALTER against the live schema. Every
        column here must be nullable or carry a DEFAULT — SQLite cannot add a
        NOT NULL column without one to a table that already has rows.
        """
        for table, columns in _ADDED_COLUMNS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, ddl in columns:
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                    logger.info(f"Migrated {table} table: added {col}")

    # ── Public API ───────────────────────────

    def create(self, job_id: str, session: dict) -> None:
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO jobs (job_id, created_at, updated_at, data) "
                "VALUES (?, ?, ?, ?)",
                (job_id, now, now, self._serialize(session)),
            )

    def get(self, job_id: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT data FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._deserialize(row[0]) if row else None

    def update(self, job_id: str, **patch) -> None:
        """Read-modify-write a job's session dict (used to cache streamed output)."""
        session = self.get(job_id)
        if session is None:
            return
        session.update(patch)
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE jobs SET data = ?, updated_at = ? WHERE job_id = ?",
                (self._serialize(session), time.time(), job_id),
            )

    def delete(self, job_id: str) -> None:
        """Drop a job once its results have been handed to the client."""
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))

    def prune(self, max_age_seconds: int = JOB_TTL_SECONDS) -> int:
        cutoff = time.time() - max_age_seconds
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
            return cur.rowcount

    def vacuum(self) -> None:
        """Reclaim file space after deletes — SQLite keeps pages otherwise.

        VACUUM cannot run inside a transaction, hence the manual connection.
        """
        with self._lock:
            c = self._conn()
            try:
                c.isolation_level = None
                c.execute("VACUUM")
            finally:
                c.close()

    def sweep(self, vacuum: bool = True) -> dict[str, int]:
        """Expire stale jobs and sessions, then reclaim the freed pages."""
        jobs = self.prune()
        sessions = self.prune_sessions()
        if vacuum and (jobs or sessions):
            self.vacuum()
        return {"jobs": jobs, "sessions": sessions}

    def count(self) -> int:
        with self._lock, self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # ── OAuth sessions ───────────────────────

    def create_session(self, session_id: str, data: dict, ttl_seconds: int) -> None:
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO sessions (session_id, created_at, expires_at, data) "
                "VALUES (?, ?, ?, ?)",
                (session_id, now, now + ttl_seconds, json.dumps(data)),
            )

    def get_session(self, session_id: str) -> dict | None:
        if not session_id:
            return None
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT data, expires_at FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        data, expires_at = row
        if expires_at < time.time():
            self.delete_session(session_id)
            return None
        return json.loads(data)

    def update_session(self, session_id: str, data: dict) -> bool:
        """Replace a live session's payload, keeping its original expiry.

        Used to count OTP attempts. Returns False if the row is gone or already
        expired, so a caller can't resurrect one by writing to it.
        """
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE sessions SET data = ? WHERE session_id = ? AND expires_at > ?",
                (json.dumps(data), session_id, time.time()),
            )
            return cur.rowcount > 0

    def touch_session(self, session_id: str, ttl_seconds: int,
                      min_extension_seconds: float | None = None) -> None:
        """Slide a session's expiry forward — an idle timeout, not a fixed one.

        This is what keeps an in-use window from being logged out mid-task: every
        authenticated request pushes the deadline back, so the TTL only ever
        elapses after genuine inactivity.

        `min_extension_seconds` keeps this from writing on every single request:
        a session touched moments ago already expires at ~now+ttl, so the WHERE
        clause finds nothing and the UPDATE is a no-op. Costs one cheap indexed
        write per session per minute instead of one per request.

        It has to stay small *relative to the TTL*, which is why it scales rather
        than sitting at a flat 60s: with a short TTL a fixed throttle exceeds the
        whole lifetime, no UPDATE ever matches, and the idle timeout silently
        degrades into a hard one that logs people out mid-task. Deployments using
        the 8h default never noticed; a 60s TTL would break outright.
        """
        if min_extension_seconds is None:
            min_extension_seconds = min(60.0, ttl_seconds / 10)
        now = time.time()
        new_expiry = now + ttl_seconds
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE sessions SET expires_at = ? "
                "WHERE session_id = ? AND expires_at > ? AND expires_at < ?",
                (new_expiry, session_id, now, new_expiry - min_extension_seconds),
            )

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def delete_sessions_for_user(self, user_id: str, keep: str | None = None) -> int:
        """Revoke every login session belonging to a user.

        A password reset has to do this: the whole point is to lock out whoever
        might already be holding a session on a compromised account, and leaving
        their old token live would defeat the reset. `keep` spares the session
        just issued to the person doing the resetting.

        The user_id lives inside the JSON payload rather than a column, so this
        filters in Python. Session counts are small (one per active login) and
        this runs only on reset/logout-everywhere, not on the hot path.
        """
        removed = 0
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT session_id, data FROM sessions").fetchall()
            for session_id, blob in rows:
                if session_id == keep:
                    continue
                try:
                    if json.loads(blob).get("user_id") != user_id:
                        continue
                except (json.JSONDecodeError, AttributeError):
                    continue
                c.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
                removed += 1
        return removed

    def prune_sessions(self) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            return cur.rowcount

    # ── Published repos ──────────────────────

    def record_published_repo(
        self,
        full_name: str,
        user_id: str,
        repo_url: str,
        *,
        test_count: int = 0,
        files_pushed: int = 0,
        cicd_added: bool = False,
        private: bool = True,
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO published_repos "
                "(full_name, user_id, repo_url, created_at, test_count, files_pushed, "
                " cicd_added, private) VALUES (?,?,?,?,?,?,?,?)",
                (full_name, user_id, repo_url, time.time(), test_count, files_pushed,
                 int(bool(cicd_added)), int(bool(private))),
            )

    def was_published_by(self, full_name: str, user_id: str) -> bool:
        """True only if THIS user published THIS repo through the app."""
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM published_repos WHERE full_name = ? AND user_id = ?",
                (full_name, user_id),
            ).fetchone()
        return row is not None

    def forget_published_repo(self, full_name: str, user_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "DELETE FROM published_repos WHERE full_name = ? AND user_id = ?",
                (full_name, user_id),
            )

    def list_published_repos(self, user_id: str, limit: int | None = None) -> list[dict]:
        sql = (
            "SELECT full_name, repo_url, created_at, test_count, files_pushed, "
            "cicd_added, private FROM published_repos "
            "WHERE user_id = ? ORDER BY created_at DESC"
        )
        params: tuple = (user_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        with self._lock, self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [
            {"full_name": r[0], "repo_url": r[1], "created_at": r[2],
             "test_count": r[3], "files_pushed": r[4],
             "cicd_added": bool(r[5]), "private": bool(r[6])}
            for r in rows
        ]

    # ── Activity history (profile) ───────────

    def record_generation(
        self,
        user_id: str,
        source: str,
        framework: str,
        language: str,
        test_count: int,
        file_count: int,
        *,
        status: str = "success",
        duration_ms: int | None = None,
        error: str = "",
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO generations "
                "(id, user_id, created_at, source, framework, language, test_count, "
                " file_count, status, duration_ms, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, user_id, time.time(), source, framework,
                 language, test_count, file_count, status, duration_ms, error or None),
            )

    def list_generations(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT created_at, source, framework, language, test_count, file_count, "
                "status, duration_ms, error "
                "FROM generations WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [
            {"created_at": r[0], "source": r[1], "framework": r[2],
             "language": r[3], "test_count": r[4], "file_count": r[5],
             "status": r[6], "duration_ms": r[7], "error": r[8]}
            for r in rows
        ]

    def record_scan(
        self,
        user_id: str,
        url: str,
        grade: str,
        score: int,
        findings: int,
        *,
        duration_ms: int | None = None,
        counts: dict | None = None,
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO scans "
                "(id, user_id, created_at, url, grade, score, findings, duration_ms, counts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, user_id, time.time(), url, grade, score, findings,
                 duration_ms, json.dumps(counts) if counts else None),
            )

    def list_scans(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT created_at, url, grade, score, findings, duration_ms, counts "
                "FROM scans WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [
            {"created_at": r[0], "url": r[1], "grade": r[2],
             "score": r[3], "findings": r[4], "duration_ms": r[5],
             "counts": json.loads(r[6]) if r[6] else {}}
            for r in rows
        ]

    def activity_totals(self, user_id: str) -> dict[str, int]:
        """Lifetime counts. Separate from list_*(), which is capped for display —
        the headline number must not silently become "20" once someone passes it."""
        with self._lock, self._conn() as c:
            gens = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(test_count), 0), "
                "COALESCE(SUM(status = 'failed'), 0) "
                "FROM generations WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            scans = c.execute(
                "SELECT COUNT(*) FROM scans WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            repos = c.execute(
                "SELECT COUNT(*) FROM published_repos WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        return {
            "generations": gens[0],
            "tests_written": gens[1],
            "generations_failed": gens[2],
            "scans": scans,
            "repos_published": repos,
        }

    def activity_feed(self, user_id: str, limit: int = 30) -> list[dict]:
        """Every feature's activity as one timeline, newest first.

        A UNION rather than three queries stitched together in Python: the three
        tables live in one database, so the sort and the LIMIT happen once inside
        SQLite instead of over-fetching from each table and re-sorting here.

        The columns carry a different meaning per feature, so they're selected
        into positional slots and named by `kind` below — the alternative, a
        table with nine mostly-NULL columns, would push that same branch into
        every reader.
        """
        sql = """
            SELECT 'generate' AS kind, created_at, source, framework,
                   test_count, file_count, status
              FROM generations WHERE user_id = ?
            UNION ALL
            SELECT 'publish', created_at, full_name, repo_url,
                   test_count, files_pushed, 'success'
              FROM published_repos WHERE user_id = ?
            UNION ALL
            SELECT 'scan', created_at, url, grade,
                   score, findings, 'success'
              FROM scans WHERE user_id = ?
             ORDER BY created_at DESC LIMIT ?
        """
        with self._lock, self._conn() as c:
            rows = c.execute(sql, (user_id, user_id, user_id, limit)).fetchall()

        feed = []
        for kind, created_at, title, detail, a, b, status in rows:
            item = {"kind": kind, "created_at": created_at, "status": status}
            if kind == "generate":
                item.update(source=title, framework=detail, test_count=a, file_count=b)
            elif kind == "publish":
                item.update(full_name=title, repo_url=detail, test_count=a, files_pushed=b)
            else:
                item.update(url=title, grade=detail, score=a, findings=b)
            feed.append(item)
        return feed

    def activity_series(self, user_id: str, days: int = 30) -> list[dict]:
        """Per-day counts for each feature over the last `days` days.

        Zero-filled, so the chart draws a continuous axis instead of skipping the
        days someone didn't use the app. Days are the *server's* local days, which
        is what 'localtime' below means — a user in a distant timezone sees their
        activity land on the server's day boundary, not their own.
        """
        since = time.time() - days * 86_400
        counts: dict[str, dict[str, int]] = {}
        with self._lock, self._conn() as c:
            for kind, table in ACTIVITY_TABLES:
                # `table` is a literal from ACTIVITY_TABLES, never user input.
                rows = c.execute(
                    f"SELECT DATE(created_at, 'unixepoch', 'localtime') AS d, COUNT(*) "
                    f"FROM {table} WHERE user_id = ? AND created_at >= ? GROUP BY d",
                    (user_id, since),
                ).fetchall()
                counts[kind] = dict(rows)

        today = date.today()
        return [
            {
                "date": day.isoformat(),
                **{kind: counts[kind].get(day.isoformat(), 0)
                   for kind, _ in ACTIVITY_TABLES},
            }
            for day in (today - timedelta(days=i) for i in range(days - 1, -1, -1))
        ]

    # ── Per-user settings ────────────────────

    DEFAULT_SETTINGS = {
        "framework": "playwright",
        "language": "typescript",
        "base_url": "http://localhost:3000",
    }

    def get_settings(self, user_id: str) -> dict:
        """This user's saved form defaults, falling back per-field.

        Missing row and NULL column both mean "never set", so both resolve to the
        same default — a user who only ever changed `framework` still gets a
        sensible language back.
        """
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT framework, language, base_url FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        saved = dict(zip(("framework", "language", "base_url"), row)) if row else {}
        return {k: saved.get(k) or v for k, v in self.DEFAULT_SETTINGS.items()}

    def save_settings(self, user_id: str, **fields) -> dict:
        """Upsert form defaults. Unknown keys are ignored; returns the new state."""
        sets = {k: v for k, v in fields.items()
                if k in self.DEFAULT_SETTINGS and v is not None}
        if sets:
            cols = ", ".join(sets)
            placeholders = ", ".join("?" for _ in sets)
            updates = ", ".join(f"{k} = excluded.{k}" for k in sets)
            with self._lock, self._conn() as c:
                c.execute(
                    f"INSERT INTO user_settings (user_id, updated_at, {cols}) "
                    f"VALUES (?, ?, {placeholders}) "
                    f"ON CONFLICT(user_id) DO UPDATE SET "
                    f"updated_at = excluded.updated_at, {updates}",
                    (user_id, time.time(), *sets.values()),
                )
        return self.get_settings(user_id)

    # ── Account deletion ─────────────────────

    def delete_account(self, user_id: str) -> dict[str, int]:
        """Erase a user and everything belonging to them. Irreversible.

        One transaction, so a crash halfway can't leave activity rows orphaned
        behind a deleted account. `jobs` is keyed by job_id with the user inside
        the JSON blob, so it's filtered in Python — those expire within 24h
        anyway, and this runs once per account, never on a hot path.
        """
        freed = {}
        with self._lock, self._conn() as c:
            for table in ("generations", "scans", "published_repos",
                          "usage", "user_settings"):
                cur = c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                freed[table] = cur.rowcount

            jobs = 0
            for job_id, blob in c.execute("SELECT job_id, data FROM jobs").fetchall():
                try:
                    if json.loads(blob).get("user_id") != user_id:
                        continue
                except (json.JSONDecodeError, AttributeError):
                    continue
                c.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                jobs += 1
            freed["jobs"] = jobs

            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        # Sessions live behind their own helper (the user_id is inside the JSON
        # payload); reuse it rather than re-implementing the scan here.
        freed["sessions"] = self.delete_sessions_for_user(user_id)
        logger.info(f"Deleted account {user_id}: {freed}")
        return freed

    # ── Users ────────────────────────────────

    _USER_KEYS = [
        "id", "email", "password_hash", "name", "github_id", "github_login",
        "avatar_url", "created_at", "plan", "plan_expires_at", "email_verified",
        "suspended", "suspended_at", "suspended_reason", "stripe_customer_id",
    ]
    _USER_COLS = ", ".join(_USER_KEYS)

    def _row_to_user(self, row) -> dict | None:
        if not row:
            return None
        user = dict(zip(self._USER_KEYS, row))
        # SQLite has no bool; hand callers a real one so `if user["email_verified"]`
        # can't be fooled by the string "0" coming back from a legacy row.
        user["email_verified"] = bool(user.get("email_verified"))
        user["suspended"] = bool(user.get("suspended"))
        return user

    def create_user(self, user: dict) -> None:
        # Placeholders are counted from _USER_KEYS rather than written out: the
        # two drifting apart is a silent column-shift, not a syntax error.
        placeholders = ",".join("?" * len(self._USER_KEYS))
        with self._lock, self._conn() as c:
            c.execute(
                f"INSERT INTO users ({self._USER_COLS}) VALUES ({placeholders})",
                (
                    user["id"], user.get("email"), user.get("password_hash"),
                    user.get("name"), user.get("github_id"), user.get("github_login"),
                    user.get("avatar_url"), user.get("created_at", time.time()),
                    user.get("plan", "free"), user.get("plan_expires_at"),
                    int(bool(user.get("email_verified", False))),
                    int(bool(user.get("suspended", False))),
                    user.get("suspended_at"), user.get("suspended_reason"),
                    user.get("stripe_customer_id"),
                ),
            )

    def get_user_by_id(self, user_id: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                f"SELECT {self._USER_COLS} FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._row_to_user(row)

    def get_user_by_email(self, email: str) -> dict | None:
        if not email:
            return None
        with self._lock, self._conn() as c:
            row = c.execute(
                f"SELECT {self._USER_COLS} FROM users WHERE email = ? COLLATE NOCASE",
                (email.strip().lower(),),
            ).fetchone()
        return self._row_to_user(row)

    def get_user_by_github(self, github_id: str) -> dict | None:
        if not github_id:
            return None
        with self._lock, self._conn() as c:
            row = c.execute(
                f"SELECT {self._USER_COLS} FROM users WHERE github_id = ?", (str(github_id),)
            ).fetchone()
        return self._row_to_user(row)

    def get_user_by_stripe_customer(self, customer_id: str) -> dict | None:
        """Who a Stripe customer id belongs to. This is how a renewal invoice —
        which carries no client_reference_id — finds its user."""
        if not customer_id:
            return None
        with self._lock, self._conn() as c:
            row = c.execute(
                f"SELECT {self._USER_COLS} FROM users WHERE stripe_customer_id = ?",
                (customer_id,),
            ).fetchone()
        return self._row_to_user(row)

    def link_stripe_customer(self, user_id: str, customer_id: str) -> None:
        """Remember which Stripe customer is this user, so later renewals resolve.

        Clears the id from any other user first: Stripe reuses one customer per
        payer, and if two accounts ever claimed the same one, a renewal lookup
        would be ambiguous and could grant Pro to the wrong account.
        """
        if not user_id or not customer_id:
            return
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE users SET stripe_customer_id = NULL "
                "WHERE stripe_customer_id = ? AND id != ?",
                (customer_id, user_id),
            )
            c.execute(
                "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
                (customer_id, user_id),
            )

    def update_user(self, user_id: str, **fields) -> None:
        allowed = {"email", "password_hash", "name", "github_id", "github_login",
                   "avatar_url", "email_verified"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        if "email_verified" in sets:
            sets["email_verified"] = int(bool(sets["email_verified"]))
        cols = ", ".join(f"{k} = ?" for k in sets)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE users SET {cols} WHERE id = ?", (*sets.values(), user_id))

    def set_suspended(self, user_id: str, suspended: bool, reason: str = "") -> None:
        """Lock or unlock an account. Sessions are the caller's job — a suspended
        user holding a live token would otherwise keep working until it expired
        (see main.admin_suspend_user, which revokes them)."""
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE users SET suspended = ?, suspended_at = ?, suspended_reason = ? "
                "WHERE id = ?",
                (int(suspended), time.time() if suspended else None,
                 (reason or "").strip()[:500] or None, user_id),
            )
        logger.info(f"Account {user_id} {'suspended' if suspended else 'unsuspended'}")

    # ── Admin queries ────────────────────────
    #
    # Cross-user reads, unlike everything above them, which is scoped to one
    # user_id. Guarded at the API layer by services.admin.require_admin — nothing
    # in here checks permissions, so never expose one of these on a route that
    # doesn't carry that dependency.

    def list_users(self, query: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
        """Users newest-first, optionally filtered by email/name/GitHub handle."""
        sql = f"SELECT {self._USER_COLS} FROM users"
        params: list = []
        if query.strip():
            # LIKE with an escaped pattern, not string interpolation: a search for
            # "100%" must find the literal text, not match every row.
            like = f"%{self._escape_like(query.strip())}%"
            sql += (" WHERE (email LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
                    "OR github_login LIKE ? ESCAPE '\\')")
            params += [like, like, like]
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [max(1, min(limit, 200)), max(0, offset)]
        with self._lock, self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._row_to_user(r) for r in rows]

    def count_users(self, query: str = "") -> int:
        sql = "SELECT COUNT(*) FROM users"
        params: list = []
        if query.strip():
            like = f"%{self._escape_like(query.strip())}%"
            sql += (" WHERE (email LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
                    "OR github_login LIKE ? ESCAPE '\\')")
            params += [like, like, like]
        with self._lock, self._conn() as c:
            return c.execute(sql, params).fetchone()[0]

    @staticmethod
    def _escape_like(text: str) -> str:
        """Neutralise LIKE wildcards so a search term matches itself literally."""
        for ch in ("\\", "%", "_"):
            text = text.replace(ch, "\\" + ch)
        return text

    def count_sessions_for_user(self, user_id: str) -> int:
        """Live (unexpired) login sessions — i.e. how many places they're signed in.

        The user_id sits inside the session JSON, so this filters in Python for
        the same reason delete_sessions_for_user does.
        """
        now = time.time()
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT data FROM sessions WHERE expires_at > ?", (now,)
            ).fetchall()
        total = 0
        for (blob,) in rows:
            try:
                if json.loads(blob).get("user_id") == user_id:
                    total += 1
            except (json.JSONDecodeError, AttributeError):
                continue
        return total

    def platform_totals(self) -> dict[str, int]:
        """Instance-wide counts for the admin overview."""
        day_ago = time.time() - 86_400
        week_ago = time.time() - 7 * 86_400
        with self._lock, self._conn() as c:
            def scalar(sql: str, params: tuple = ()) -> int:
                return c.execute(sql, params).fetchone()[0] or 0

            totals = {
                "users": scalar("SELECT COUNT(*) FROM users"),
                "users_new_7d": scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (week_ago,)),
                "users_suspended": scalar("SELECT COUNT(*) FROM users WHERE suspended = 1"),
                # Effective plan, not the stored column — a lapsed Pro is a free
                # user, and counting them as paying would overstate revenue.
                "users_pro": scalar(
                    "SELECT COUNT(*) FROM users WHERE plan = 'pro' "
                    "AND (plan_expires_at IS NULL OR plan_expires_at > ?)",
                    (time.time(),),
                ),
                "generations": scalar("SELECT COUNT(*) FROM generations"),
                "generations_24h": scalar("SELECT COUNT(*) FROM generations WHERE created_at >= ?", (day_ago,)),
                "generations_failed": scalar("SELECT COUNT(*) FROM generations WHERE status != 'success'"),
                "scans": scalar("SELECT COUNT(*) FROM scans"),
                "published_repos": scalar("SELECT COUNT(*) FROM published_repos"),
                "active_sessions": scalar("SELECT COUNT(*) FROM sessions WHERE expires_at > ?", (time.time(),)),
            }
        return totals

    def platform_breakdown(self, column: str, limit: int = 8) -> list[dict]:
        """Generation counts grouped by framework or language, biggest first."""
        if column not in {"framework", "language"}:
            raise ValueError(f"cannot group generations by {column!r}")
        with self._lock, self._conn() as c:
            # `column` is validated against a literal set above, never interpolated
            # from raw caller input.
            rows = c.execute(
                f"SELECT COALESCE(NULLIF({column}, ''), 'unknown') AS k, COUNT(*) AS n "
                f"FROM generations GROUP BY k ORDER BY n DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"name": k, "count": n} for k, n in rows]

    def platform_series(self, days: int = 30) -> list[dict]:
        """Per-day activity across every user — the all-tenant twin of
        activity_series, zero-filled the same way and on the same server-local
        day boundary, so the two charts can be read against each other."""
        since = time.time() - days * 86_400
        counts: dict[str, dict[str, int]] = {}
        with self._lock, self._conn() as c:
            for kind, table in ACTIVITY_TABLES:
                # `table` is a literal from ACTIVITY_TABLES, never user input.
                rows = c.execute(
                    f"SELECT DATE(created_at, 'unixepoch', 'localtime') AS d, COUNT(*) "
                    f"FROM {table} WHERE created_at >= ? GROUP BY d",
                    (since,),
                ).fetchall()
                counts[kind] = dict(rows)
            signups = dict(c.execute(
                "SELECT DATE(created_at, 'unixepoch', 'localtime') AS d, COUNT(*) "
                "FROM users WHERE created_at >= ? GROUP BY d",
                (since,),
            ).fetchall())

        today = date.today()
        return [
            {
                "date": day.isoformat(),
                **{kind: counts[kind].get(day.isoformat(), 0) for kind, _ in ACTIVITY_TABLES},
                "signups": signups.get(day.isoformat(), 0),
            }
            for day in (today - timedelta(days=i) for i in range(days - 1, -1, -1))
        ]

    def recent_failures(self, limit: int = 20) -> list[dict]:
        """Failed generations across all users — the admin's first stop when
        something is broken, and not visible from any per-user view."""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT g.id, g.user_id, u.email, g.created_at, g.source, g.framework, "
                "       g.status, g.error, g.duration_ms "
                "FROM generations g LEFT JOIN users u ON u.id = g.user_id "
                "WHERE g.status != 'success' ORDER BY g.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ("id", "user_id", "email", "created_at", "source", "framework",
                "status", "error", "duration_ms")
        return [dict(zip(keys, r)) for r in rows]

    def set_usage(self, user_id: str, period: str, count: int) -> int:
        """Force a user's usage counter for a period. Admin override for support
        ('your run failed, here's the credit back'). Returns the new value."""
        count = max(0, count)
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO usage (user_id, period, count) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, period) DO UPDATE SET count = excluded.count",
                (user_id, period, count),
            )
        logger.info(f"Usage for {user_id} in {period} set to {count} by admin")
        return count

    # ── Plans & metered usage ────────────────

    def get_plan(self, user_id: str) -> str:
        """The user's *effective* plan right now — a lapsed paid plan reads
        as 'free' without needing a background job to downgrade it."""
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT plan, plan_expires_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if not row:
            return "free"
        plan, expires_at = row
        if expires_at is not None and expires_at < time.time():
            return "free"
        return plan or "free"

    def set_plan(self, user_id: str, plan: str, expires_at: float | None = None) -> None:
        """Grant or revoke a plan. Call this from a *verified* payment webhook —
        never straight from a client request."""
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE users SET plan = ?, plan_expires_at = ? WHERE id = ?",
                (plan, expires_at, user_id),
            )
        logger.info(f"Plan for {user_id} set to {plan} (expires={expires_at})")

    def get_usage(self, user_id: str, period: str) -> int:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT count FROM usage WHERE user_id = ? AND period = ?",
                (user_id, period),
            ).fetchone()
        return row[0] if row else 0

    def try_consume(self, user_id: str, period: str, limit: int | None) -> tuple[bool, int]:
        """Atomically reserve one unit of quota.

        Returns (allowed, used_after). Check and increment share a transaction so
        concurrent requests can't both pass a check on the same last credit.
        `limit=None` means unlimited — still counted, for usage reporting.
        """
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT count FROM usage WHERE user_id = ? AND period = ?",
                (user_id, period),
            ).fetchone()
            used = row[0] if row else 0
            if limit is not None and used >= limit:
                return False, used
            c.execute(
                "INSERT INTO usage (user_id, period, count) VALUES (?, ?, 1) "
                "ON CONFLICT(user_id, period) DO UPDATE SET count = count + 1",
                (user_id, period),
            )
            return True, used + 1

    def refund(self, user_id: str, period: str) -> None:
        """Hand back a reserved unit when the work it paid for failed."""
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE usage SET count = MAX(0, count - 1) WHERE user_id = ? AND period = ?",
                (user_id, period),
            )

    # ── (De)serialization ────────────────────

    _FR_TAG = "__filterresult__"

    def _serialize(self, session: dict) -> str:
        s = dict(session)
        fr = s.get("filter_result")
        if isinstance(fr, FilterResult):
            s["filter_result"] = {self._FR_TAG: True, **asdict(fr)}
        return json.dumps(s)

    def _deserialize(self, blob: str) -> dict:
        s = json.loads(blob)
        fr = s.get("filter_result")
        if isinstance(fr, dict) and fr.get(self._FR_TAG):
            fields = {k: v for k, v in fr.items() if k != self._FR_TAG}
            s["filter_result"] = FilterResult(**fields)
        return s


# Singleton imported by the API layer.
store = JobStore()
