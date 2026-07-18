"""
importance_model.py — a learned scorer for "how testable is this file".

`file_extractor.score_file` ranks files with hand-written rules: a router is a
10, a page is a 9, a util is a 4. Those rules are good priors but they are
frozen — they cannot notice that in *this* codebase the interesting behaviour
lives in `features/` and the `pages/` are thin shells. This module learns that
distinction from data.

It is a logistic-regression classifier implemented from scratch — no numpy, no
sklearn, just the math — for three reasons that matter here:

  1. **It's honest about being ML.** Every weight is a named coefficient on a
     named feature, fit by gradient descent and written to JSON you can read.
     Nothing is hidden in a binary blob or a third-party black box.
  2. **It has no dependencies.** The backend ships as-is; adding a training
     stack for one small model would be a poor trade.
  3. **It's re-fittable from real outcomes.** The label at bootstrap is weak
     supervision (does the file carry anchors a test could ground against — see
     services/grounding.py), but the same `fit` re-trains on *real* signal once
     the self-heal loop and the benchmark harness have produced it: which files
     actually led to verified, low-fragility selectors.

The model degrades safely: with no trained artifact on disk, callers fall back
to the heuristic `score_file`, so importing this module changes nothing until a
model is trained and saved.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


MODEL_PATH = os.path.join(os.path.dirname(__file__), "importance_model.json")

# Feature names are the model's vocabulary; the weight vector is aligned to this
# order. Adding a feature means retraining, so the list is the contract.
FEATURES = [
    "is_router",         # matches a router/entrypoint pattern
    "in_pages_dir",      # pages/app/views/screens
    "in_components_dir",
    "in_layouts_dir",
    "has_auth_keyword",  # auth/login/signup/checkout/cart in the path
    "is_config",
    "is_test_file",      # already a test — low value as a *target*
    "is_style_or_asset",
    "ext_component",     # .tsx/.jsx/.vue/.svelte
    "path_depth",        # normalized directory depth
    "n_ids",             # stable anchors present in the file body
    "n_testids",
    "n_forms",
    "n_buttons",
    "n_inputs",
    "has_onclick",
    "size_small",        # tiny files are usually not the interesting screen
]


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def extract_features(path: str, content: str = "") -> dict[str, float]:
    """Cheap, path- and body-derived features. No parsing, no LLM."""
    p = path.lower()
    name = Path(p).name
    parts = p.split("/")
    body = content or ""

    def count(pat: str) -> int:
        return len(re.findall(pat, body, re.IGNORECASE))

    n_ids = count(r'\bid=["\']')
    n_testids = count(r'data-(?:testid|cy)=')
    n_forms = count(r'<form\b')
    n_buttons = count(r'<button\b|role=["\']button')
    n_inputs = count(r'<input\b|<select\b|<textarea\b')

    return {
        "is_router": 1.0 if re.search(r'(?:^|/)(?:app|router|routes|index)\.(?:tsx?|jsx?|vue)$', p) else 0.0,
        "in_pages_dir": 1.0 if any(d in parts for d in ("pages", "app", "views", "screens")) else 0.0,
        "in_components_dir": 1.0 if any(d in parts for d in ("components", "containers", "features")) else 0.0,
        "in_layouts_dir": 1.0 if any(d in parts for d in ("layouts", "layout")) else 0.0,
        "has_auth_keyword": 1.0 if any(k in p for k in ("auth", "login", "signup", "checkout", "cart", "form")) else 0.0,
        "is_config": 1.0 if re.search(r'\.(config|conf)\.|(?:^|/)(?:package\.json|tsconfig\.json|vite\.config)', name) else 0.0,
        "is_test_file": 1.0 if re.search(r'\.(spec|test)\.|__tests__|\.cy\.', p) else 0.0,
        "is_style_or_asset": 1.0 if re.search(r'\.(css|scss|less|svg|png|jpg|ico|woff2?)$', p) else 0.0,
        "ext_component": 1.0 if re.search(r'\.(tsx|jsx|vue|svelte)$', p) else 0.0,
        # Depth normalized so a 4-deep path ~= 1.0; keeps the weight interpretable.
        "path_depth": _clip01((len(parts) - 1) / 5.0),
        "n_ids": _clip01(n_ids / 8.0),
        "n_testids": _clip01(n_testids / 8.0),
        "n_forms": _clip01(n_forms / 3.0),
        "n_buttons": _clip01(n_buttons / 8.0),
        "n_inputs": _clip01(n_inputs / 8.0),
        "has_onclick": 1.0 if re.search(r'onclick|@click|v-on:click|addEventListener', body, re.IGNORECASE) else 0.0,
        "size_small": 1.0 if len(body) < 300 else 0.0,
    }


@dataclass
class ImportanceModel:
    weights: dict = field(default_factory=dict)
    bias: float = 0.0
    trained_on: int = 0          # number of samples the current weights were fit on

    # ── inference ────────────────────────────────────────────────────────────
    def predict_proba(self, features: dict[str, float]) -> float:
        z = self.bias + sum(self.weights.get(f, 0.0) * features.get(f, 0.0) for f in FEATURES)
        return 1.0 / (1.0 + math.exp(-z))

    def score_1_to_10(self, path: str, content: str = "") -> int:
        """Map probability to the 1–10 scale the extractor already speaks."""
        p = self.predict_proba(extract_features(path, content))
        return max(1, min(10, round(1 + p * 9)))

    # ── training (batch gradient descent) ────────────────────────────────────
    def fit(self, samples: list[tuple[dict, int]], *, epochs: int = 400,
            lr: float = 0.3, l2: float = 1e-3) -> "ImportanceModel":
        """Fit weights on (features, label∈{0,1}) pairs.

        Plain full-batch logistic regression with L2 regularization. Small data,
        small model — no minibatching or fancy optimizer earns its complexity.
        """
        if not samples:
            return self
        for f in FEATURES:
            self.weights.setdefault(f, 0.0)
        n = len(samples)
        for _ in range(epochs):
            grad = {f: 0.0 for f in FEATURES}
            gbias = 0.0
            for feats, label in samples:
                pred = self.predict_proba(feats)
                err = pred - label
                for f in FEATURES:
                    grad[f] += err * feats.get(f, 0.0)
                gbias += err
            for f in FEATURES:
                # gradient + L2 shrinkage
                self.weights[f] -= lr * (grad[f] / n + l2 * self.weights[f])
            self.bias -= lr * (gbias / n)
        self.trained_on = n
        return self

    # ── persistence ──────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"weights": self.weights, "bias": self.bias,
                "trained_on": self.trained_on, "features": FEATURES}

    def save(self, path: str = MODEL_PATH) -> None:
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "ImportanceModel":
        return cls(weights=dict(d.get("weights", {})),
                   bias=float(d.get("bias", 0.0)),
                   trained_on=int(d.get("trained_on", 0)))

    @classmethod
    def load(cls, path: str = MODEL_PATH) -> Optional["ImportanceModel"]:
        if not os.path.exists(path):
            return None
        try:
            with open(path) as fh:
                return cls.from_dict(json.load(fh))
        except Exception:
            return None


# ── weak-supervision labels for bootstrap training ───────────────────────────

def weak_label(path: str, content: str) -> int:
    """A cheap, honest label for the cold-start: is this a file a UI test would
    actually target?

    Positive when the file is UI-facing AND carries something a test could
    anchor to (a form, a button, ids/test-ids). Negative for configs, styles,
    assets, tests, and bodies with no interactive surface. This is deliberately
    weak — it is the prior we bootstrap from, to be replaced by real grounding
    outcomes once we have them.
    """
    f = extract_features(path, content)
    if f["is_config"] or f["is_style_or_asset"] or f["is_test_file"]:
        return 0
    interactive = f["n_forms"] + f["n_buttons"] + f["n_inputs"] + f["n_testids"] + f["has_onclick"]
    ui_facing = f["is_router"] or f["in_pages_dir"] or f["in_components_dir"] or f["has_auth_keyword"]
    return 1 if (ui_facing and interactive > 0) else 0


def build_dataset(repos: list[dict[str, str]]) -> list[tuple[dict, int]]:
    """Turn {path: content} repos into (features, weak_label) training rows."""
    rows: list[tuple[dict, int]] = []
    for files in repos:
        for path, content in files.items():
            rows.append((extract_features(path, content), weak_label(path, content)))
    return rows


def train_and_save(repos: list[dict[str, str]], path: str = MODEL_PATH) -> ImportanceModel:
    model = ImportanceModel().fit(build_dataset(repos))
    model.save(path)
    return model


# Loaded once at import if a trained artifact exists; None otherwise so callers
# fall back to the heuristic. Kept module-level so the extractor pays the load
# cost once, not per file.
_ACTIVE: Optional[ImportanceModel] = ImportanceModel.load()


def active_model() -> Optional[ImportanceModel]:
    return _ACTIVE


def learned_score(path: str, content: str, fallback: int) -> int:
    """Model score when a model is trained; the caller's heuristic otherwise."""
    m = _ACTIVE
    return m.score_1_to_10(path, content) if m is not None else fallback
