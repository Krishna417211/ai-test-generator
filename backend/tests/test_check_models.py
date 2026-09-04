"""Tests for the model-availability guard.

Only the offline logic is exercised — which model ids the script expects and
which keys it will use. The network calls are the point of the script and are
deliberately not mocked into an assertion here: a test that mocks the provider
listing would be testing the mock, which is precisely the blind spot the script
exists to cover.
"""
import importlib.util
import pathlib
import sys

import pytest

from services.llm_router import MODELS, Provider

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_models.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("check_models", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["check_models"] = m
    spec.loader.exec_module(m)
    return m


def test_expects_every_model_the_router_actually_uses(mod):
    """The guard must cover the real table, not a copy of it.

    If a provider/tier row is added to MODELS and this drifts, the new model is
    unguarded — which is the whole failure being defended against.
    """
    expected: dict[Provider, set[str]] = {}
    for (provider, _tier), spec in MODELS.items():
        expected.setdefault(provider, set()).add(spec.id)
    assert mod.wanted() == expected


def test_every_provider_has_a_way_to_list_its_models(mod):
    """A provider in MODELS with no lister would be silently unchecked."""
    for provider in mod.wanted():
        assert provider in mod.LISTERS, f"no model lister for {provider}"


def test_reads_keys_from_the_env_prefix_the_router_uses(mod, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_1", "one")
    monkeypatch.setenv("GROQ_API_KEY_3", "three")   # gaps are fine
    monkeypatch.delenv("GROQ_API_KEY_2", raising=False)
    found = dict(mod.keys_for(Provider.GROQ))
    assert found["GROQ_API_KEY_1"] == "one"
    assert found["GROQ_API_KEY_3"] == "three"
    assert "GROQ_API_KEY_2" not in found


def test_blank_and_whitespace_keys_are_ignored(mod, monkeypatch):
    """An unset key in .env is often '' or a stray space, not absent."""
    monkeypatch.setenv("GEMINI_API_KEY_1", "")
    monkeypatch.setenv("GEMINI_API_KEY_2", "   ")
    assert mod.keys_for(Provider.GEMINI) == []


def test_returns_2_when_nothing_is_configured(mod, monkeypatch):
    """Exit 2 is 'could not check', which must not read as 'all clear'."""
    for provider in mod.wanted():
        for i in range(1, 11):
            monkeypatch.delenv(f"{mod.ENV_PREFIX[provider]}{i}", raising=False)
    monkeypatch.setattr(sys, "argv", ["check_models.py", "--quiet"])
    assert mod.main() == 2
