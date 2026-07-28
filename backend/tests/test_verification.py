"""test_verification.py — Email verification links, password reset links, and
the login OTP.

These are the checks that decide who gets into an account, so the tests lean on
the failure paths (reuse, expiry, wrong code, lockout, cross-namespace replay)
rather than just the happy path.
"""

import re
import time
import asyncio

import pytest

from config import settings
from services import verification
from services.store import JobStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway store, swapped in everywhere verification reaches for it."""
    s = JobStore(db_path=str(tmp_path / "v.db"))
    monkeypatch.setattr(verification, "store", s)
    return s


@pytest.fixture
def outbox(monkeypatch):
    """Capture emails instead of sending them."""
    sent: list[dict] = []

    async def fake_send(to, subject, text, html=None):
        sent.append({"to": to, "subject": subject, "text": text, "html": html})

    monkeypatch.setattr(verification, "send_email", fake_send)
    return sent


def _code_from(email: dict) -> str:
    return re.search(r"\b(\d{6})\b", email["text"]).group(1)


def _link_token(email: dict) -> str:
    return re.search(r"token=([A-Za-z0-9_\-]+)", email["text"]).group(1)


class TestMaskEmail:
    def test_masks_local_part_only(self):
        assert verification.mask_email("ada@example.com") == "a**@example.com"

    def test_single_char_local_still_masked(self):
        # Must not return "a@example.com" — that would leak the whole address.
        assert verification.mask_email("a@example.com") == "a*@example.com"

    def test_junk_is_not_echoed_back(self):
        assert verification.mask_email("not-an-email") == ""
        assert verification.mask_email("") == ""


class TestEmailVerification:
    def test_roundtrip(self, store, outbox):
        asyncio.run(verification.send_verification_email("usr_1", "ada@example.com", "Ada"))
        assert outbox[0]["to"] == "ada@example.com"
        token = _link_token(outbox[0])
        assert verification.consume_verification_token(token) == "usr_1"

    def test_token_is_single_use(self, store, outbox):
        asyncio.run(verification.send_verification_email("usr_1", "ada@example.com"))
        token = _link_token(outbox[0])
        verification.consume_verification_token(token)
        with pytest.raises(verification.InvalidCode):
            verification.consume_verification_token(token)

    def test_garbage_token_rejected(self, store):
        with pytest.raises(verification.InvalidCode):
            verification.consume_verification_token("made-up")
        with pytest.raises(verification.InvalidCode):
            verification.consume_verification_token("")

    def test_token_is_not_stored_in_the_clear(self, store, outbox):
        """A dumped sessions table must not be a list of working links."""
        asyncio.run(verification.send_verification_email("usr_1", "ada@example.com"))
        token = _link_token(outbox[0])
        with store._conn() as c:
            blob = " ".join(str(r) for r in c.execute("SELECT * FROM sessions"))
        assert token not in blob

    def test_expired_token_rejected(self, store, outbox, monkeypatch):
        monkeypatch.setattr(settings, "email_verify_ttl_seconds", -1)
        asyncio.run(verification.send_verification_email("usr_1", "ada@example.com"))
        with pytest.raises(verification.InvalidCode):
            verification.consume_verification_token(_link_token(outbox[0]))


class TestPasswordReset:
    def test_roundtrip_and_single_use(self, store, outbox):
        asyncio.run(verification.send_password_reset_email("usr_2", "grace@example.com"))
        token = _link_token(outbox[0])
        assert verification.consume_reset_token(token) == "usr_2"
        with pytest.raises(verification.InvalidCode):
            verification.consume_reset_token(token)

    def test_verify_token_cannot_be_used_as_a_reset_token(self, store, outbox):
        """The namespaces exist precisely to stop this: a link that only proves
        'this address exists' must not be redeemable for a password change."""
        asyncio.run(verification.send_verification_email("usr_1", "ada@example.com"))
        token = _link_token(outbox[0])
        with pytest.raises(verification.InvalidCode):
            verification.consume_reset_token(token)

    def test_reset_token_cannot_be_used_as_a_verify_token(self, store, outbox):
        asyncio.run(verification.send_password_reset_email("usr_2", "grace@example.com"))
        token = _link_token(outbox[0])
        with pytest.raises(verification.InvalidCode):
            verification.consume_verification_token(token)


class TestLoginOtp:
    def test_correct_code_returns_user(self, store, outbox):
        ch = asyncio.run(verification.start_login_challenge("usr_3", "ada@example.com"))
        code = _code_from(outbox[0])
        assert verification.verify_login_challenge(ch.challenge_id, code) == "usr_3"

    def test_code_is_six_digits_and_in_the_subject(self, store, outbox):
        asyncio.run(verification.start_login_challenge("usr_3", "ada@example.com"))
        code = _code_from(outbox[0])
        assert len(code) == 6 and code.isdigit()
        assert code in outbox[0]["subject"]      # visible in the inbox list

    def test_code_is_single_use(self, store, outbox):
        ch = asyncio.run(verification.start_login_challenge("usr_3", "ada@example.com"))
        code = _code_from(outbox[0])
        verification.verify_login_challenge(ch.challenge_id, code)
        with pytest.raises(verification.InvalidCode):
            verification.verify_login_challenge(ch.challenge_id, code)

    def test_code_is_hashed_at_rest(self, store, outbox):
        asyncio.run(verification.start_login_challenge("usr_3", "ada@example.com"))
        code = _code_from(outbox[0])
        with store._conn() as c:
            blob = " ".join(str(r) for r in c.execute("SELECT * FROM sessions"))
        assert code not in blob

    def test_wrong_code_burns_an_attempt_then_locks_out(self, store, outbox, monkeypatch):
        monkeypatch.setattr(settings, "login_otp_max_attempts", 3)
        ch = asyncio.run(verification.start_login_challenge("usr_3", "ada@example.com"))
        code = _code_from(outbox[0])
        wrong = "000000" if code != "000000" else "111111"

        for _ in range(2):
            with pytest.raises(verification.InvalidCode):
                verification.verify_login_challenge(ch.challenge_id, wrong)

        # Third failure burns the challenge itself...
        with pytest.raises(verification.InvalidCode, match="Too many"):
            verification.verify_login_challenge(ch.challenge_id, wrong)
        # ...so even the right code is now worthless.
        with pytest.raises(verification.InvalidCode):
            verification.verify_login_challenge(ch.challenge_id, code)

    def test_malformed_code_rejected_without_burning_the_challenge(self, store, outbox):
        """Non-numeric input skips bcrypt (it can't match anyway) but still
        counts — otherwise it'd be a free way to keep a challenge alive."""
        ch = asyncio.run(verification.start_login_challenge("usr_3", "ada@example.com"))
        code = _code_from(outbox[0])
        with pytest.raises(verification.InvalidCode):
            verification.verify_login_challenge(ch.challenge_id, "abcdef")
        # The real code still works — one bad guess isn't fatal.
        assert verification.verify_login_challenge(ch.challenge_id, code) == "usr_3"

    def test_unknown_challenge_rejected(self, store):
        with pytest.raises(verification.InvalidCode):
            verification.verify_login_challenge("nope", "123456")

    def test_expired_challenge_rejected(self, store, outbox, monkeypatch):
        monkeypatch.setattr(settings, "login_otp_ttl_seconds", -1)
        ch = asyncio.run(verification.start_login_challenge("usr_3", "ada@example.com"))
        with pytest.raises(verification.InvalidCode):
            verification.verify_login_challenge(ch.challenge_id, _code_from(outbox[0]))

    def test_challenge_id_does_not_contain_the_user_id(self, store, outbox):
        """It's handed to a caller who hasn't authenticated yet."""
        ch = asyncio.run(verification.start_login_challenge("usr_3", "ada@example.com"))
        assert "usr_3" not in ch.challenge_id

    def test_two_challenges_get_different_codes_and_ids(self, store, outbox):
        a = asyncio.run(verification.start_login_challenge("usr_3", "ada@example.com"))
        b = asyncio.run(verification.start_login_challenge("usr_3", "ada@example.com"))
        assert a.challenge_id != b.challenge_id
        # A code minted for one challenge must not open the other.
        code_a = _code_from(outbox[0])
        if code_a != _code_from(outbox[1]):
            with pytest.raises(verification.InvalidCode):
                verification.verify_login_challenge(b.challenge_id, code_a)


class TestSessionLifetime:
    # These read the sessions table directly to observe expiry, which means they
    # have to spell the storage key the way the store does: sessions are keyed by
    # sha256 of the token, never the token (services/store.py _skey). Querying
    # session_id='tok' matches nothing.

    """The 'stay logged in until the window closes' half: an *idle* timeout."""

    def _store(self, tmp_path):
        return JobStore(db_path=str(tmp_path / "s.db"))

    def test_touch_extends_an_active_session(self, tmp_path):
        s = self._store(tmp_path)
        s.create_session("tok", {"user_id": "u1"}, ttl_seconds=100)
        with s._conn() as c:
            before = c.execute(
                "SELECT expires_at FROM sessions WHERE session_id=?", (s._skey("tok"),)
            ).fetchone()[0]

        s.touch_session("tok", ttl_seconds=1000, min_extension_seconds=0)
        with s._conn() as c:
            after = c.execute(
                "SELECT expires_at FROM sessions WHERE session_id=?", (s._skey("tok"),)
            ).fetchone()[0]
        assert after > before

    def test_touch_is_throttled(self, tmp_path):
        """A just-touched session must not be rewritten on every request."""
        s = self._store(tmp_path)
        s.create_session("tok", {"user_id": "u1"}, ttl_seconds=1000)
        with s._conn() as c:
            before = c.execute(
                "SELECT expires_at FROM sessions WHERE session_id=?", (s._skey("tok"),)
            ).fetchone()[0]

        s.touch_session("tok", ttl_seconds=1000, min_extension_seconds=60)
        with s._conn() as c:
            after = c.execute(
                "SELECT expires_at FROM sessions WHERE session_id=?", (s._skey("tok"),)
            ).fetchone()[0]
        assert after == before

    def test_short_ttl_still_slides(self, tmp_path):
        """Regression: the write-throttle must scale with the TTL.

        With a flat 60s throttle and a TTL below it, `expires_at < now+ttl-60`
        can never be true, so the UPDATE silently matched nothing and the idle
        timeout became a hard timeout — an actively-used session got logged out
        mid-task. Caught by driving a 6s-TTL server, not by the unit tests, which
        all passed while passing min_extension_seconds explicitly.
        """
        s = self._store(tmp_path)
        s.create_session("tok", {"user_id": "u1"}, ttl_seconds=6)
        with s._conn() as c:
            before = c.execute(
                "SELECT expires_at FROM sessions WHERE session_id=?", (s._skey("tok"),)
            ).fetchone()[0]

        time.sleep(1.1)
        s.touch_session("tok", ttl_seconds=6)      # default throttle
        with s._conn() as c:
            after = c.execute(
                "SELECT expires_at FROM sessions WHERE session_id=?", (s._skey("tok"),)
            ).fetchone()[0]
        assert after > before, "a short-TTL session must still slide"

    def test_touch_cannot_resurrect_an_expired_session(self, tmp_path):
        s = self._store(tmp_path)
        s.create_session("tok", {"user_id": "u1"}, ttl_seconds=-1)
        s.touch_session("tok", ttl_seconds=1000, min_extension_seconds=0)
        assert s.get_session("tok") is None

    def test_update_session_keeps_expiry_and_refuses_dead_rows(self, tmp_path):
        s = self._store(tmp_path)
        s.create_session("tok", {"user_id": "u1", "attempts": 0}, ttl_seconds=100)
        with s._conn() as c:
            before = c.execute(
                "SELECT expires_at FROM sessions WHERE session_id=?", (s._skey("tok"),)
            ).fetchone()[0]

        assert s.update_session("tok", {"user_id": "u1", "attempts": 1}) is True
        assert s.get_session("tok")["attempts"] == 1
        with s._conn() as c:
            after = c.execute(
                "SELECT expires_at FROM sessions WHERE session_id=?", (s._skey("tok"),)
            ).fetchone()[0]
        assert after == before

        s.create_session("dead", {"user_id": "u1"}, ttl_seconds=-1)
        assert s.update_session("dead", {"user_id": "u1"}) is False

    def test_delete_sessions_for_user_is_scoped(self, tmp_path):
        s = self._store(tmp_path)
        s.create_session("a1", {"user_id": "u1"}, ttl_seconds=100)
        s.create_session("a2", {"user_id": "u1"}, ttl_seconds=100)
        s.create_session("b1", {"user_id": "u2"}, ttl_seconds=100)

        assert s.delete_sessions_for_user("u1") == 2
        assert s.get_session("a1") is None and s.get_session("a2") is None
        assert s.get_session("b1") is not None       # another user is untouched

    def test_delete_sessions_for_user_can_keep_one(self, tmp_path):
        """A password reset revokes everything *except* the session it just
        issued to the person doing the resetting."""
        s = self._store(tmp_path)
        s.create_session("old", {"user_id": "u1"}, ttl_seconds=100)
        s.create_session("new", {"user_id": "u1"}, ttl_seconds=100)
        assert s.delete_sessions_for_user("u1", keep="new") == 1
        assert s.get_session("old") is None
        assert s.get_session("new") is not None

    def test_non_login_rows_are_not_collateral(self, tmp_path):
        """OAuth states and OTP challenges share this table and have no user_id."""
        s = self._store(tmp_path)
        s.create_session("oauthstate:x", {"kind": "oauth_state"}, ttl_seconds=100)
        s.create_session("mine", {"user_id": "u1"}, ttl_seconds=100)
        s.delete_sessions_for_user("u1")
        assert s.get_session("oauthstate:x") is not None


class TestEmailVerifiedColumn:
    def test_new_accounts_default_to_unverified(self, tmp_path):
        """create_user must not hand out a verified account by omission."""
        s = JobStore(db_path=str(tmp_path / "u.db"))
        s.create_user({"id": "u1", "email": "a@b.com", "created_at": time.time()})
        assert s.get_user_by_id("u1")["email_verified"] is False

    def test_verification_can_be_set_and_read_as_bool(self, tmp_path):
        s = JobStore(db_path=str(tmp_path / "u.db"))
        s.create_user({"id": "u1", "email": "a@b.com", "created_at": time.time()})
        s.update_user("u1", email_verified=True)
        assert s.get_user_by_id("u1")["email_verified"] is True

    def test_accounts_predating_the_column_are_grandfathered(self, tmp_path):
        """Pre-existing rows migrate to verified=1. Defaulting them to 0 would
        lock out every existing user the moment this shipped."""
        db = str(tmp_path / "legacy.db")
        s = JobStore(db_path=db)
        # Simulate a row written before the column existed.
        with s._conn() as c:
            c.execute("ALTER TABLE users DROP COLUMN email_verified")
            c.execute(
                "INSERT INTO users (id, email, created_at, plan) VALUES (?,?,?,?)",
                ("old", "old@b.com", time.time(), "free"),
            )
        reopened = JobStore(db_path=db)          # re-runs _migrate
        assert reopened.get_user_by_id("old")["email_verified"] is True
