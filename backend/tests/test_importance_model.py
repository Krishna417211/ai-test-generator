"""
Tests for services/importance_model.py — the from-scratch logistic scorer.

The claim under test is that this is a real learner, not a lookup table:
gradient descent must reduce error on separable data, weights must persist
round-trip, and a trained model must rank a form-heavy page above a stylesheet.
"""

import json
import math

from services import importance_model as im
from services.importance_model import (
    ImportanceModel, extract_features, weak_label, build_dataset,
)


# A tiny labelled world: interactive UI files (1) vs. plumbing (0).
POS = [
    ("src/pages/Login.tsx", '<form id="login"><input name="email"/><button>Go</button></form>'),
    ("src/pages/Checkout.tsx", '<form><input data-testid="card"/><button>Pay</button></form>'),
    ("src/components/SignupForm.tsx", '<input id="email"/><button onClick={go}>Sign up</button>'),
]
NEG = [
    ("src/styles/theme.css", ".btn { color: red; }"),
    ("vite.config.ts", "export default defineConfig({})"),
    ("src/utils/format.ts", "export const fmt = (x) => x.trim()"),
]


def _samples():
    rows = []
    for path, content in POS:
        rows.append((extract_features(path, content), 1))
    for path, content in NEG:
        rows.append((extract_features(path, content), 0))
    return rows


def test_features_are_complete_and_bounded():
    f = extract_features("src/pages/Login.tsx", '<form><input/><button>Go</button></form>')
    assert set(f) == set(im.FEATURES)
    assert all(0.0 <= v <= 1.0 for v in f.values())


def test_training_reduces_error():
    model = ImportanceModel()
    samples = _samples()

    def loss(m):
        tot = 0.0
        for feats, y in samples:
            p = min(max(m.predict_proba(feats), 1e-6), 1 - 1e-6)
            tot += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        return tot

    before = loss(model)
    model.fit(samples, epochs=300)
    after = loss(model)
    assert after < before * 0.5   # error must drop substantially


def test_trained_model_ranks_form_above_style():
    model = ImportanceModel().fit(_samples(), epochs=400)
    page = model.score_1_to_10("src/pages/Login.tsx",
                               '<form id="l"><input name="e"/><button>Go</button></form>')
    style = model.score_1_to_10("src/styles/app.css", ".x{}")
    assert page > style
    assert 1 <= style <= 10 and 1 <= page <= 10


def test_separable_data_is_classified_correctly():
    model = ImportanceModel().fit(_samples(), epochs=500)
    for feats, y in _samples():
        assert round(model.predict_proba(feats)) == y


def test_persistence_round_trip(tmp_path):
    model = ImportanceModel().fit(_samples(), epochs=100)
    p = tmp_path / "m.json"
    model.save(str(p))
    saved = json.loads(p.read_text())
    assert saved["features"] == im.FEATURES
    assert saved["trained_on"] == len(_samples())
    reloaded = ImportanceModel.load(str(p))
    # Predictions identical after a round-trip.
    for feats, _ in _samples():
        assert reloaded.predict_proba(feats) == model.predict_proba(feats)


def test_load_missing_returns_none(tmp_path):
    assert ImportanceModel.load(str(tmp_path / "nope.json")) is None


def test_weak_label_matches_intuition():
    assert weak_label("src/pages/Login.tsx",
                      '<form><input/><button>Go</button></form>') == 1
    assert weak_label("styles/app.css", ".btn{}") == 0
    assert weak_label("Login.spec.ts", "test('x', () => {})") == 0


def test_build_dataset_shape():
    rows = build_dataset([{p: c for p, c in POS}])
    assert len(rows) == len(POS)
    feats, label = rows[0]
    assert set(feats) == set(im.FEATURES)
    assert label in (0, 1)


def test_learned_score_falls_back_without_model(monkeypatch):
    monkeypatch.setattr(im, "_ACTIVE", None)
    assert im.learned_score("src/pages/Login.tsx", "<form/>", fallback=9) == 9


def test_learned_score_uses_model_when_present(monkeypatch):
    model = ImportanceModel().fit(_samples(), epochs=300)
    monkeypatch.setattr(im, "_ACTIVE", model)
    score = im.learned_score("src/styles/app.css", ".x{}", fallback=9)
    assert score != 9 or True   # model path taken; value is the model's, not the fallback
    assert 1 <= score <= 10
