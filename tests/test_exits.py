"""Tests for trailing SL math + paper end-to-end exit behaviour."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.broker.base import Quote  # noqa: E402
from src.broker.paper import PaperBroker  # noqa: E402
from src.config import load_config  # noqa: E402
from src.db.store import Store  # noqa: E402
from src.engine.execution import ExecutionManager, trailing_sl  # noqa: E402
from src.engine.risk import RiskManager  # noqa: E402
from src.engine.signal_engine import TradeIntent  # noqa: E402
from src.strategies.base import Signal  # noqa: E402


def test_trailing_sl_basic():
    # entry 300, sl 210 => R = 90
    # below arm (profit < 1R): unchanged
    assert trailing_sl(300, 210, 330, 30, 90, 1.0, 0.5, 0.8) == 210
    # at profit 1R with peak 390: sl = 390 - 45 = 345
    assert trailing_sl(300, 210, 390, 90, 90, 1.0, 0.5, 0.8) == 345
    # never decreases
    assert trailing_sl(300, 345, 380, 80, 90, 1.0, 0.5, 0.8) == 345
    # breakeven: profit 0.9R -> sl at least entry
    assert trailing_sl(300, 210, 382, 81, 90, 1.0, 0.5, 0.8) == 300
    # peak trail ratchets further
    assert trailing_sl(300, 345, 450, 150, 90, 1.0, 0.5, 0.8) == 405


class FakeQuotes:
    def __init__(self):
        self.quotes = {}

    def set(self, sid, ltp):
        self.quotes[sid] = Quote(security_id=sid, symbol=sid, ltp=ltp,
                                 bid=ltp * 0.998, ask=ltp * 1.002)

    def __call__(self, sid):
        return self.quotes.get(sid)


def test_paper_trail_and_reversal_exit():
    cfg = load_config()
    cfg["mode"] = "paper"
    store = Store("data/test_exits.db")
    risk = RiskManager(cfg, store, lot_sizes={"NIFTY": 65})
    broker = PaperBroker(500000.0)
    fq = FakeQuotes()
    exec_mgr = ExecutionManager(broker, risk, store, "paper")
    exec_mgr.set_quote_provider(fq)

    fq.set("S1", 300.0)
    intent = TradeIntent(
        signal=Signal(strategy="momentum", side="BUY", option_type="CE", underlying="NIFTY",
                      expiry="2026-08-21", strike=24500, reason="t", ts=time.time(),
                      entry_price_hint=300.0,
                      meta={"greeks": {"delta": 0.5}, "max_hold_min": 0}),
        security_id="S1", premium_entry=300.0, qty=65, sl_price=210.0,
        target_price=462.0, lot_size=65, product_type="INTRADAY",
    )
    ok, _ = exec_mgr.enter(intent)
    assert ok
    assert broker.positions["S1"].sl_price == 210.0

    # 1) price rises to 1R profit (peak 390): trail arms -> SL moves to 345
    fq.set("S1", 390.0)
    exec_mgr.monitor(indicators={"NIFTY": {}})
    assert broker.positions["S1"].sl_price == 345.0

    # 2) reversal confirmation (5m EMA flip) - must persist 2 minutes
    fq.set("S1", 400.0)
    exec_mgr.monitor(indicators={"NIFTY": {"ema_fast_5m": 10.0, "ema_slow_5m": 11.0}})
    assert "S1" in broker.positions      # flip detected but not yet confirmed
    exec_mgr.open["S1"].meta["flip_ts"] = time.time() - 121   # simulate 2+ minutes
    exec_mgr.monitor(indicators={"NIFTY": {"ema_fast_5m": 10.0, "ema_slow_5m": 11.0}})
    assert "S1" not in broker.positions
    trades = store.all_trades(10)
    assert trades and trades[0]["exit_reason"] == "REVERSAL_EXIT"
    assert trades[0]["pnl"] > 0


def test_btst_hold_and_exit():
    cfg = load_config()
    cfg["mode"] = "paper"
    store = Store("data/test_btst.db")
    risk = RiskManager(cfg, store, lot_sizes={"NIFTY": 65})
    broker = PaperBroker(500000.0)
    fq = FakeQuotes()
    exec_mgr = ExecutionManager(broker, risk, store, "paper")
    exec_mgr.set_quote_provider(fq)

    fq.set("B1", 250.0)
    intent = TradeIntent(
        signal=Signal(strategy="momentum", side="BUY", option_type="CE", underlying="NIFTY",
                      expiry="2026-08-27", strike=24400, reason="t", ts=time.time(),
                      entry_price_hint=250.0, meta={"btst": True, "greeks": {"delta": 0.5}}),
        security_id="B1", premium_entry=250.0, qty=65, sl_price=175.0,
        target_price=385.0, lot_size=65, product_type="INTRADAY",
    )
    ok, _ = exec_mgr.enter(intent)
    assert ok
    tr = exec_mgr.open["B1"]
    assert tr.btst is True
    assert intent.product_type == "MARGIN"        # overnight product

    # time exit must NOT flatten BTST trades
    exec_mgr.check_time_exit()
    assert "B1" in exec_mgr.open

    # intraday-only flatten must leave it alone too
    exec_mgr.exit_non_btst("EOD")
    assert "B1" in exec_mgr.open

    # dedicated btst exit closes it
    exec_mgr.exit_btst("BTST_OPEN_EXIT")
    assert "B1" not in exec_mgr.open
    trades = store.all_trades(10)
    assert trades and trades[0]["exit_reason"] == "BTST_OPEN_EXIT"


if __name__ == "__main__":
    test_trailing_sl_basic()
    print("ok test_trailing_sl_basic")
    test_paper_trail_and_reversal_exit()
    print("ok test_paper_trail_and_reversal_exit")
    test_btst_hold_and_exit()
    print("ok test_btst_hold_and_exit")
    print("ALL EXIT TESTS PASSED")
