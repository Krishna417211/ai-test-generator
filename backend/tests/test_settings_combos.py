"""A saved framework/language pair must be one the writer can actually generate.

`TestFramework` and `Language` each validate on their own, which is not the same
as validating the PAIR. "cypress" and "java" are both legal values and together
are a suite that does not exist — and because
`WriterAgent._normalize_framework` maps any non-Java Selenium request to
`selenium_python`, an impossible pair did not fail loudly. It quietly generated
a different language from the one the user chose.
"""
import tempfile

import pytest
from fastapi.testclient import TestClient

import main
import ratelimit
from models.schemas import FRAMEWORK_LANGUAGES, combo_error
from services import auth as auth_svc
from services.store import JobStore


@pytest.fixture(autouse=True)
def _fresh_limiters():
    for name in dir(ratelimit):
        lim = getattr(ratelimit, name)
        if isinstance(lim, ratelimit.SlidingWindowLimiter):
            lim._hits.clear()
    yield


@pytest.fixture
def client(monkeypatch):
    store = JobStore(tempfile.mktemp(suffix=".db"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(auth_svc, "store", store)
    monkeypatch.setattr(main.settings, "session_secret", "test-secret-for-encryption")
    c = TestClient(main.app)
    c.store = store
    return c


def _signed_in(client, user_id="u1"):
    client.store.create_user({"id": user_id, "email": f"{user_id}@example.com",
                              "password_hash": "x", "email_verified": 1})
    return {"Authorization": f"Bearer {auth_svc.create_login_session(user_id)}"}


class TestTheTableMatchesWhatCanBeBuilt:
    def test_selenium_offers_no_javascript(self):
        """The bug this file exists for: selenium+js silently became Python."""
        assert "javascript" not in FRAMEWORK_LANGUAGES["selenium"]
        assert FRAMEWORK_LANGUAGES["selenium"] == {"python", "java"}

    def test_cypress_is_js_only(self):
        assert FRAMEWORK_LANGUAGES["cypress"] == {"typescript", "javascript"}

    @pytest.mark.parametrize("fw,lang", [
        ("playwright", "typescript"), ("playwright", "javascript"),
        ("playwright", "python"), ("cypress", "typescript"),
        ("cypress", "javascript"), ("selenium", "python"), ("selenium", "java"),
    ])
    def test_every_real_combination_is_accepted(self, fw, lang):
        assert combo_error(fw, lang) is None

    @pytest.mark.parametrize("fw,lang", [
        ("cypress", "java"), ("cypress", "python"),
        ("selenium", "typescript"), ("selenium", "javascript"),
    ])
    def test_impossible_combinations_are_named(self, fw, lang):
        msg = combo_error(fw, lang)
        assert msg and lang.title() in msg
        # The message has to say what IS available, or the user is just stuck.
        assert any(ok in msg for ok in FRAMEWORK_LANGUAGES[fw])

    def test_an_unknown_framework_is_left_alone(self):
        """The enum already ruled on the framework; don't second-guess it."""
        assert combo_error("nextgen-framework", "cobol") is None


class TestTheEndpointRefusesThem:
    def test_saving_an_impossible_pair_is_rejected(self, client):
        headers = _signed_in(client)
        r = client.put("/api/settings",
                       json={"framework": "selenium", "language": "typescript"},
                       headers=headers)
        assert r.status_code == 400, r.text
        assert "Selenium" in r.json()["detail"]

    def test_a_real_pair_is_saved(self, client):
        headers = _signed_in(client)
        r = client.put("/api/settings",
                       json={"framework": "selenium", "language": "java"},
                       headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["framework"] == "selenium"
        assert r.json()["language"] == "java"

    def test_a_partial_patch_is_checked_against_what_is_stored(self, client):
        """The half that isn't in the request still decides whether it's valid.

        Validating only within one request would let `{"language": "java"}` land
        on a stored framework of cypress — which is the same broken pair arrived
        at in two steps.
        """
        headers = _signed_in(client)
        assert client.put("/api/settings",
                          json={"framework": "cypress", "language": "typescript"},
                          headers=headers).status_code == 200

        r = client.put("/api/settings", json={"language": "java"}, headers=headers)
        assert r.status_code == 400, r.text
        assert "Cypress" in r.json()["detail"]

        # And the rejected write left the stored settings untouched.
        assert client.get("/api/settings", headers=headers).json()["language"] == "typescript"

    def test_changing_framework_and_language_together_is_allowed(self, client):
        """Moving cypress+ts -> selenium+java must work in one request.

        If the pair were checked one field at a time, this legitimate change
        would be impossible: whichever field was read first would be paired with
        the other's old value.
        """
        headers = _signed_in(client)
        client.put("/api/settings", json={"framework": "cypress", "language": "typescript"},
                   headers=headers)
        r = client.put("/api/settings", json={"framework": "selenium", "language": "java"},
                       headers=headers)
        assert r.status_code == 200, r.text
        assert (r.json()["framework"], r.json()["language"]) == ("selenium", "java")

    def test_settings_unrelated_to_the_pair_still_save(self, client):
        headers = _signed_in(client)
        r = client.put("/api/settings", json={"base_url": "https://example.com"},
                       headers=headers)
        assert r.status_code == 200, r.text
