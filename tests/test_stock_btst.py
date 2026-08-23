"""Tests for the stock BTST screener and equity tracking."""
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.broker.base import Quote  # noqa: E402
from src.config import load_config  # noqa: E402
from src.db.store import Store  # noqa: E402
from src.engine.execution import ExecutionManager  # noqa: E402
from src.engine.risk import RiskManager  # noqa: E402
from src.broker.paper import PaperBroker  # noqa: E402
from src.screener.stock_screener import StockScreener  # noqa: E402


class FakeMaster:
    def stock_security_id(self, sym):
        return {"GOODSTK": "111", "BADSTK": "222"}.get(sym.upper())


class FakeData:
    def __init__(self):
        self.rows = {}

    def historical_daily(self, sid, seg, itype, from_d, to_d):
        return self.rows.get(sid, [])


def _daily_rows(up: bool, vol_surge: bool = True, base=100.0, n=70):
    rows = []
    ts = int(time.time()) - n * 86400
    price = base
    for i in range(n):
        if up:
            # gentle alternating gains/dips so RSI stays in the 52-72 band
            price *= (1.002 if i % 2 == 0 else 0.9988) if i > 20 else 1.0
        else:
            price *= 0.997 if i > 20 else 1.0
        rows.append({"timestamp": ts + i * 86400, "open": price, "high": price * 1.01,
                     "low": price * 0.99, "close": price,
                     "volume": 2100000.0 if (vol_surge and i == n - 1) else 1000000.0})
    return rows


def test_screener_picks_qualifier():
    data = FakeData()
    data.rows["111"] = _daily_rows(up=True, vol_surge=True)   # trending + volume surge
    data.rows["222"] = _daily_rows(up=False, vol_surge=True)  # downtrend -> reject
    cfg = load_config()
    cfg["stock_btst"]["universe"] = ["GOODSTK", "BADSTK"]
    s = StockScreener(data, FakeMaster(), cfg)
    picks = s.screen()
    assert picks, "qualifier should be picked"
    assert all(p.symbol == "GOODSTK" for p in picks)
    assert picks[0].vol_ratio >= 1.5
    assert 52 <= picks[0].rsi <= 72


def test_track_stock_and_sl_exit():
    cfg = load_config()
    cfg["mode"] = "paper"
    store = Store("data/test_stock.db")
    risk = RiskManager(cfg, store, lot_sizes={})
    broker = PaperBroker(500000.0)
    fq = {}

    def qp(sid):
        return fq.get(sid)

    exec_mgr = ExecutionManager(broker, risk, store, "paper")
    exec_mgr.set_quote_provider(qp)
    fq["111"] = Quote(security_id="111", symbol="GOODSTK", ltp=100.0)

    ok, _ = exec_mgr.track_stock("111", "GOODSTK", 100, 100.0, sl_price=97.5, target_price=105.0)
    assert ok
    assert exec_mgr.open["111"].segment == "EQ"
    assert broker.positions["111"].sl_price == 97.5

    # price drops to SL -> exits with STOCK_SL_HIT
    fq["111"] = Quote(security_id="111", symbol="GOODSTK", ltp=97.0)
    exec_mgr.monitor()
    assert "111" not in exec_mgr.open
    trades = store.all_trades(10)
    assert trades and trades[0]["exit_reason"] == "STOCK_SL_HIT"
    assert trades[0]["pnl"] < 0


if __name__ == "__main__":
    test_screener_picks_qualifier()
    print("ok test_screener_picks_qualifier")
    test_track_stock_and_sl_exit()
    print("ok test_track_stock_and_sl_exit")
    print("ALL STOCK BTST TESTS PASSED")
