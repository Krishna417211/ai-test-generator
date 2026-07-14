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
import sqlite3
import logging
from threading import Lock
from dataclasses import asdict

from agents.filter_agent import FilterResult

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testgen.db"
)

# Jobs older than this are pruned on startup (24h).
JOB_TTL_SECONDS = int(os.getenv("TESTGEN_JOB_TTL", str(24 * 60 * 60)))


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
                    full_name  TEXT NOT NULL,
                    user_id    TEXT NOT NULL,
                    repo_url   TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (full_name, user_id)
                )
                """
            )
            self._migrate(c)
        logger.info(f"JobStore ready at {self.db_path}")

    def _migrate(self, c: sqlite3.Connection) -> None:
        """Add columns to tables that already exist in deployed databases.

        CREATE TABLE IF NOT EXISTS silently skips a table that's already there,
        so new columns need an explicit ALTER against the live schema.
        """
        have = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        for col, ddl in (
            ("plan", "plan TEXT NOT NULL DEFAULT 'free'"),
            # Epoch seconds; NULL means the plan doesn't expire. A lapsed
            # plan_expires_at reads as 'free' without a write (see get_plan).
            ("plan_expires_at", "plan_expires_at REAL"),
        ):
            if col not in have:
                c.execute(f"ALTER TABLE users ADD COLUMN {ddl}")
                logger.info(f"Migrated users table: added {col}")

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

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def prune_sessions(self) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            return cur.rowcount

    # ── Published repos ──────────────────────

    def record_published_repo(self, full_name: str, user_id: str, repo_url: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO published_repos (full_name, user_id, repo_url, created_at) "
                "VALUES (?, ?, ?, ?)",
                (full_name, user_id, repo_url, time.time()),
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

    def list_published_repos(self, user_id: str) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT full_name, repo_url, created_at FROM published_repos "
                "WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [{"full_name": r[0], "repo_url": r[1], "created_at": r[2]} for r in rows]

    # ── Users ────────────────────────────────

    _USER_KEYS = [
        "id", "email", "password_hash", "name", "github_id", "github_login",
        "avatar_url", "created_at", "plan", "plan_expires_at",
    ]
    _USER_COLS = ", ".join(_USER_KEYS)

    def _row_to_user(self, row) -> dict | None:
        if not row:
            return None
        return dict(zip(self._USER_KEYS, row))

    def create_user(self, user: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                f"INSERT INTO users ({self._USER_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    user["id"], user.get("email"), user.get("password_hash"),
                    user.get("name"), user.get("github_id"), user.get("github_login"),
                    user.get("avatar_url"), user.get("created_at", time.time()),
                    user.get("plan", "free"), user.get("plan_expires_at"),
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

    def update_user(self, user_id: str, **fields) -> None:
        allowed = {"email", "password_hash", "name", "github_id", "github_login", "avatar_url"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE users SET {cols} WHERE id = ?", (*sets.values(), user_id))

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
