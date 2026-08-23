"""Train the ML win-probability gate from labeled trades in the SQLite log.

Run after paper trading for a while (or after a backtest that writes to the
trade db). Requires >= ml_gate.min_train_samples labeled samples.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.db.store import Store  # noqa: E402
from src.ml.gate import MLGate  # noqa: E402


def main():
    cfg = load_config()
    store = Store(cfg.get("db_path", "data/trader.db"))
    rows = store.ml_samples(labeled_only=True)
    print(f"labeled samples: {len(rows)}")
    if not rows:
        print("no labeled samples yet - trade in paper mode (or run a backtest) first")
        return
    gate = MLGate()
    ok = gate.train([{"features": r["features"], "label": r["label"]} for r in rows])
    if ok:
        print("model:", gate.meta)
        print("gate is now ACTIVE - signals below threshold %.2f will be blocked" %
              float(cfg.get("ml_gate", {}).get("threshold", 0.55)))
    else:
        print("training failed or not enough samples")


if __name__ == "__main__":
    main()
