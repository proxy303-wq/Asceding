"""Tests for the level-primary strategy (price lines decide, indicators confirm)."""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.analytics import greeks as g  # noqa: E402
from src.analytics.levels import key_levels, nearest_levels  # noqa: E402
from src.broker.base import ChainSnapshot, OptionRow  # noqa: E402
from src.market.candles import Candle, CandleSeries  # noqa: E402
from src.strategies.base import StrategyContext  # noqa: E402
from src.strategies.levels import LevelPrimaryStrategy  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SPOT = 24100.0


def _chain(spot=SPOT):
    snap = ChainSnapshot(underlying="NIFTY", expiry="2026-08-27", spot=spot, ts=int(time.time()))
    atm = round(spot / 50) * 50
    for k in range(-6, 7):
        strike = atm + k * 50
        for ot, cp in (("CE", "C"), ("PE", "P")):
            prem = g.bs_price(cp, spot, strike, 5 / 365.0, 0.065, 0.13)
            snap.rows[(strike, ot)] = OptionRow(security_id=f"S{strike}{ot}",
                symbol=f"{strike} {ot}", underlying="NIFTY", expiry="2026-08-27",
                strike=strike, option_type=ot, ltp=round(prem, 2), oi=90000.0, volume=6000.0,
                iv=0.13, delta=0.5, theta=-4.0)
    return snap


def _series(closes):
    s = CandleSeries(interval_sec=300)
    base = int(time.time()) - len(closes) * 300
    for i, c in enumerate(closes):
        s.candles.append(Candle(ts=base + i * 300, open=c, high=c * 1.001, low=c * 0.999, close=c))
    return s


def test_key_levels_collection():
    ind = {"pd_high": 24200.0, "pd_low": 24000.0, "or_high": 24150.0, "or_low": 24050.0,
           "vwap_1m": 24100.0}
    levels = key_levels(ind)
    prices = {round(l.price, 1) for l in levels}
    assert 24000.0 in prices and 24200.0 in prices
    res, sup = nearest_levels(24100.0, levels, look=150.0)
    assert res is not None and res.price > 24100.0
    assert sup is not None and sup.price < 24100.0


def test_level_support_hold_buys_call():
    ind = {"pd_low": 24050.0, "pd_high": 24200.0, "or_low": 24060.0, "or_high": 24150.0,
           "vwap_1m": 24090.0, "atr_1m": 15.0, "atr_ma_1m": 14.0,
           "ema_fast_5m": 24100.0, "ema_slow_5m": 24080.0, "rsi_1m": 55.0}
    closes = [24100.0 - i * 8 for i in range(10)] + [24055.0]      # pull into support zone
    closes[-1] = 24062.0                                            # wick below 24060, close above
    s = _series(closes)
    s.candles[-1].low = 24052.0
    ctx = StrategyContext(
        underlying="NIFTY", spot=24062.0,
        ts=int(datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0).timestamp()),
        chain=_chain(24062.0), iv_percentile=40.0, series_1m=s, series_5m=s,
        indicators=ind, config={"name": "NIFTY", "strike_interval": 50, "lot_size": 65},
    )
    sigs = LevelPrimaryStrategy({}, {"enabled": True}).evaluate(ctx)
    assert sigs, "support hold near pd_low/or_low should signal BUY CE"
    assert sigs[0].option_type == "CE" and "SUPPORT_HOLD" in sigs[0].reason


if __name__ == "__main__":
    test_key_levels_collection()
    print("ok test_key_levels_collection")
    test_level_support_hold_buys_call()
    print("ok test_level_support_hold_buys_call")
    print("ALL LEVEL TESTS PASSED")
