"""Run the auto-trader in LIVE mode (real orders on DHAN).

REQUIRED before starting:
  1. DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN set (data APIs need an active Data plan)
  2. Your public IP whitelisted in DhanHQ settings (order APIs REQUIRE this)
  3. TRADING_MODE=live (or edit config.yaml mode: live)
Start with paper mode for at least a few weeks first.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TRADING_MODE", "live")

from src.engine.loop import AutoTrader  # noqa: E402

if __name__ == "__main__":
    print(">>> LIVE MODE: real orders will be placed on DHAN. Proceed only if you accept the risk.")
    answer = input("Type LIVE to confirm: ").strip().upper()
    if answer != "LIVE":
        print("aborted")
        sys.exit(1)
    trader = AutoTrader()
    trader.start()
