"""Tests for the ML win-probability gate (pure-sklearn, no network)."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.ml.features import FEATURE_NAMES, extract_features, feature_dict  # noqa: E402
from src.ml.gate import MLGate  # noqa: E402


def _rows(n=60, seed=1):
    import random
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        # synthetic: high RSI + positive ema gap + high trend_score -> win more often
        rsi = rng.uniform(30, 80)
        ema_gap = rsi - 50 + rng.gauss(0, 5)
        trend = 0.5 + ema_gap / 100.0 + rng.gauss(0, 0.15)
        label = 1 if (rsi > 55 and trend > 0.55) else 0
        feat = [rsi, ema_gap, ema_gap * 0.8, 0.3, 0.5, 40.0, trend,
                float(i % 11 + 9), 3.0, 1.2, 0.5, 1.8, 0.1, 2.0, 0.002, 0.004]
        rows.append({"features": feat, "label": label, "strategy": "momentum"})
    return rows


def test_feature_names_aligned():
    assert len(FEATURE_NAMES) == 16
    fd = feature_dict([1.0] * 16)
    assert fd["rsi_1m"] == 1.0 and "theta_pct" in fd


def test_gate_trains_and_predicts():
    gate = MLGate(model_path=ROOT / "data" / "test_ml.pkl")
    gate.model_path = ROOT / "data" / "test_ml.pkl"
    rows = _rows(80)
    assert gate.train(rows) is True
    assert gate.ready
    prob_win = gate.predict_proba(rows[0]["features"])
    assert prob_win is not None and 0.0 <= prob_win <= 1.0
    ok, prob = gate.should_take(rows[0]["features"], threshold=0.0)   # never blocks
    assert ok is True and prob is not None
    # sanity: a high-prob row scores above a low-prob row on average
    import random
    rng = random.Random(3)
    hi = gate.predict_proba([62.0, 12.0, 10.0, 0.3, 0.5, 40.0, 0.7,
                             11.0, 3.0, 1.2, 0.5, 1.8, 0.1, 2.0, 0.002, 0.004])
    lo = gate.predict_proba([35.0, -12.0, -10.0, 0.3, 0.5, 40.0, 0.3,
                             14.0, 3.0, 1.2, 0.5, 1.8, 0.1, 2.0, 0.002, 0.004])
    assert hi is not None and lo is not None
    assert hi > lo, (hi, lo)
    # cleanup
    import os
    for p in (ROOT / "data" / "test_ml.pkl", ROOT / "data" / "ml_model.json"):
        if p.exists():
            os.remove(p)


def test_gate_noop_without_model():
    gate = MLGate(model_path=ROOT / "data" / "definitely_missing.pkl")
    assert gate.ready is False
    ok, prob = gate.should_take([0.0] * 16, threshold=0.55)
    assert ok is True and prob is None      # no model -> never blocks


if __name__ == "__main__":
    test_feature_names_aligned()
    print("ok test_feature_names_aligned")
    test_gate_trains_and_predicts()
    print("ok test_gate_trains_and_predicts")
    test_gate_noop_without_model()
    print("ok test_gate_noop_without_model")
    print("ALL ML TESTS PASSED")
