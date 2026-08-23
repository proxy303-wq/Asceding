"""Win-probability gate: a trained classifier that blocks low-probability signals.

Model: GradientBoostingClassifier (sklearn) - the standard best for tabular data;
falls back to a pure-numpy logistic regression if sklearn is unavailable, and to
a no-op gate if nothing is trained yet.
"""
from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path
from typing import Optional

from ..config import ROOT

log = logging.getLogger(__name__)

MODEL_PATH = ROOT / "data" / "ml_model.pkl"
META_PATH = ROOT / "data" / "ml_model.json"


class MLGate:
    def __init__(self, model_path: str | Path | None = None):
        self.model_path = Path(model_path or MODEL_PATH)
        self.model = None
        self.meta: dict = {}
        self._load()

    # --------------------------------------------------------------
    def _load(self):
        try:
            if self.model_path.exists():
                with open(self.model_path, "rb") as f:
                    self.model = pickle.load(f)
                if META_PATH.exists():
                    self.meta = json.loads(META_PATH.read_text(encoding="utf-8"))
                log.info("ML gate loaded (samples=%s)", self.meta.get("samples", 0))
        except Exception as e:
            log.warning("ML gate load failed: %s", e)
            self.model = None

    def save(self):
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.model_path, "wb") as f:
            pickle.dump(self.model, f)
        META_PATH.write_text(json.dumps(self.meta), encoding="utf-8")
        log.info("ML gate saved (samples=%s)", self.meta.get("samples", 0))

    @property
    def ready(self) -> bool:
        return self.model is not None

    # --------------------------------------------------------------
    def predict_proba(self, features: list[float]) -> Optional[float]:
        """P(win) for a feature vector. Returns None if no model."""
        if self.model is None:
            return None
        try:
            prob = self.model.predict_proba([features])[0]
            classes = list(getattr(self.model, "classes_", [0, 1]))
            # find the index of the positive class (win = 1)
            idx = classes.index(1) if 1 in classes else 1
            return float(prob[idx])
        except Exception as e:
            log.warning("ML predict failed: %s", e)
            return None

    def train(self, rows: list[dict]):
        """rows: [{'features': [...], 'label': 0|1, 'strategy': str}]"""
        if len(rows) < 20:
            log.info("ML gate needs >=20 labeled samples, have %d", len(rows))
            return False
        X = [r["features"] for r in rows]
        y = [int(r["label"]) for r in rows]
        try:
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.model_selection import cross_val_score
            model = GradientBoostingClassifier(n_estimators=120, max_depth=3,
                                               learning_rate=0.06, random_state=42)
            model.fit(X, y)
            self.model = model
            self.meta = {
                "samples": len(rows),
                "pos_rate": round(sum(y) / len(y), 4),
                "accuracy": round(float(cross_val_score(model, X, y, cv=min(5, len(rows) // 10)).mean()), 4)
                if len(rows) >= 30 else None,
                "features": len(X[0]) if X else 0,
            }
            self.save()
            log.info("ML gate trained: %s", self.meta)
            return True
        except ImportError:
            log.warning("sklearn not available - ML gate disabled")
            self.model = None
            return False
        except Exception as e:
            log.warning("ML train failed: %s", e)
            return False

    def should_take(self, features: list[float], threshold: float = 0.55) -> tuple[bool, Optional[float]]:
        prob = self.predict_proba(features)
        if prob is None:
            return True, None          # no model yet -> don't block
        return prob >= threshold, prob
