"""test_auth.py — Unit tests for password hashing, validation, and user store."""

import time

import pytest
from fastapi import HTTPException

from services import auth
from services.store import JobStore


class TestPasswordHashing:
    def test_hash_and_verify_roundtrip(self):
        h = auth.hash_password("supersecret1")
        assert h != "supersecret1"
        assert auth.verify_password("supersecret1", h)

    def test_wrong_password_fails(self):
        h = auth.hash_password("supersecret1")
        assert not auth.verify_password("wrongpass", h)

    def test_verify_handles_bad_hash(self):
        assert not auth.verify_password("x", "")
        assert not auth.verify_password("x", "not-a-bcrypt-hash")


class TestSecretEncryption:
    def test_roundtrip_with_key(self, monkeypatch):
        monkeypatch.setattr(auth.settings, "session_secret", "unit-test-secret")
        auth._fernet_cache.clear()
        enc = auth.encrypt_secret("ghp_token123")
        assert enc.startswith("enc:v1:")
        assert "ghp_token123" not in enc            # actually encrypted at rest
        assert auth.decrypt_secret(enc) == "ghp_token123"

    def test_legacy_plaintext_passthrough(self, monkeypatch):
        monkeypatch.setattr(auth.settings, "session_secret", "unit-test-secret")
        auth._fernet_cache.clear()
        assert auth.decrypt_secret("ghp_legacy_plaintext") == "ghp_legacy_plaintext"

    def test_noop_without_key(self, monkeypatch):
        monkeypatch.setattr(auth.settings, "session_secret", "")
        auth._fernet_cache.clear()
        assert auth.encrypt_secret("x") == "x"


class TestValidation:
    def test_valid_email_normalized(self):
        assert auth.validate_email("  Ada@Example.COM ") == "ada@example.com"

    @pytest.mark.parametrize("bad", ["", "no-at", "a@b", "a@b.", "@b.com"])
    def test_invalid_email_rejected(self, bad):
        with pytest.raises(HTTPException):
            auth.validate_email(bad)

    def test_short_password_rejected(self):
        with pytest.raises(HTTPException):
            auth.validate_password("short")

    def test_ok_password(self):
        auth.validate_password("longenough")  # no raise


class TestUserStore:
    def _store(self, tmp_path):
        return JobStore(db_path=str(tmp_path / "t.db"))

    def test_create_and_fetch_by_email_and_id(self, tmp_path):
        s = self._store(tmp_path)
        uid = auth.new_user_id()
        s.create_user({
            "id": uid, "email": "grace@example.com",
            "password_hash": auth.hash_password("hopper1234"),
            "name": "Grace", "created_at": time.time(),
        })
        assert s.get_user_by_id(uid)["email"] == "grace@example.com"
        assert s.get_user_by_email("GRACE@example.com")["id"] == uid  # case-insensitive
        assert s.get_user_by_email("nobody@example.com") is None

    def test_github_link_and_lookup(self, tmp_path):
        s = self._store(tmp_path)
        uid = auth.new_user_id()
        s.create_user({"id": uid, "email": "g@x.com", "created_at": time.time()})
        s.update_user(uid, github_id="12345", github_login="grace")
        assert s.get_user_by_github("12345")["id"] == uid

    def test_public_user_hides_password(self, tmp_path):
        pub = auth.public_user({"id": "u1", "email": "a@b.com", "password_hash": "secret", "name": "A"})
        assert "password_hash" not in pub
        assert pub["has_github"] is False
