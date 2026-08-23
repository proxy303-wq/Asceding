"""Push the loop's live state to the Git repo so a hosted Streamlit dashboard
can read it (e.g. raw.githubusercontent or jsDelivr CDN).

Usage (run from cron/loop at any interval, e.g. every 5-10 min during market hours):
    python scripts/sync_remote.py [--message "sync"]
Note: state.json carries no credentials - only market state/equity/positions.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", default="sync: live state snapshot")
    args = ap.parse_args()

    src = ROOT / "data" / "sync" / "state.json"
    if not src.exists():
        print("no sync state yet (loop must run once during market hours)")
        return

    dst = ROOT / "sync"
    dst.mkdir(exist_ok=True)
    shutil.copy2(src, dst / "state.json")
    # also copy a trades snapshot for the dashboard's closed-trades table
    try:
        from src.config import load_config as _lc
        from src.db.store import Store
        import csv
        store = Store(_lc().get("db_path", "data/trader.db"))
        trades = store.all_trades(200)
        with open(dst / "trades.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "symbol", "strategy", "entry_price", "exit_price",
                        "quantity", "pnl", "exit_reason"])
            for t in trades:
                w.writerow([t["ts"], t["symbol"], t["strategy"], t["entry_price"],
                            t["exit_price"], t["quantity"], t["pnl"], t["exit_reason"]])
    except Exception as e:
        print("trades snapshot failed (optional):", e)

    git("add", "-f", "sync/state.json", "sync/trades.csv")
    git("commit", "-m", args.message, "--no-verify")
    push = git("push", "origin", "main")
    if push.returncode != 0:
        print("push failed:", push.stderr[-500:])
        sys.exit(1)
    print("sync pushed. Dashboard URL (jsDelivr, ~10 min cache):")
    from src.config import load_config as _lc2
    repo = _lc2().get("remote_state_url", "")
    print("   ", repo or "(set remote_state_url in config.yaml)")


if __name__ == "__main__":
    main()
