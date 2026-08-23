"""Tests for candlestick pattern detection + the candlestick strategy."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.analytics.patterns import (analyze_candles, single_bar_pattern,
                                    three_bar_pattern, two_bar_pattern)  # noqa: E402
from src.market.candles import Candle, CandleSeries  # noqa: E402
from src.strategies.base import StrategyContext  # noqa: E402
from src.strategies.candlestick import CandlestickStrategy  # noqa: E402

CFG = {}


def single(o, h, l, c):
    return single_bar_pattern(o, h, l, c, CFG)


def test_single_patterns():
    assert single(100, 101.5, 98, 101) == "hammer"          # long lower wick
    assert single(101, 103, 99.5, 100) == "shooting_star"   # long upper wick
    assert single(100, 101, 99, 100) == "doji"              # tiny body
    assert single(100, 102, 100, 102) == "marubozu"         # no wicks
    assert single_bar_pattern(100, 100.2, 98, 100, CFG) == "dragonfly_doji"
    assert single_bar_pattern(100, 102, 99.8, 100, CFG) == "gravestone_doji"


def test_two_bar_patterns():
    assert two_bar_pattern(102, 102, 99, 100, 99, 103, 99, 103, CFG) == "bullish_engulfing"
    assert two_bar_pattern(100, 102, 100, 102, 103, 103, 99, 99, CFG) == "bearish_engulfing"
    assert two_bar_pattern(104, 104, 99, 100, 98.9, 103, 98.9, 103, CFG) == "piercing_line"


def test_three_bar_patterns():
    # morning star: big bear, small middle, big bull closing above midpoint
    assert three_bar_pattern(104, 104, 99, 99, 99.5, 100, 99.2, 99.8,
                             99.5, 103, 99.2, 102.5, CFG) == "morning_star"
    # three white soldiers
    assert three_bar_pattern(100, 101, 99.8, 100.9, 100.9, 102, 100.7, 101.9,
                             101.9, 103, 101.7, 102.9, CFG) == "three_white_soldiers"


def test_analyze_series_detects_engulfing():
    opens = [100, 102, 99]
    highs = [101, 102, 103]
    lows = [99, 99, 99]
    closes = [100, 100, 103]
    pats = analyze_candles(opens, highs, lows, closes)
    names = {p["pattern"] for p in pats}
    assert "bullish_engulfing" in names


def _series_with_hammer_after_downtrend():
    s = CandleSeries(interval_sec=300)
    base = 23600.0
    for i in range(12):
        price = base - i * 20.0
        s.candles.append(Candle(ts=int(time.time()) - (12 - i) * 300,
                                open=price + 5, high=price + 10,
                                low=price - 10, close=price))
    # final hammer bar: long lower wick after the drop
    s.candles.append(Candle(ts=int(time.time()), open=23380.0, high=23400.0,
                            low=23280.0, close=23395.0))
    return s


def test_candlestick_strategy_fires_on_hammer():
    # build chain inline (same helper shape as test_selection)
    from src.broker.base import ChainSnapshot, OptionRow
    from src.analytics import greeks as g
    snap = ChainSnapshot(underlying="NIFTY", expiry="2026-08-21",
                         spot=23395.0, ts=int(time.time()))
    for k in range(-6, 7):
        strike = 23400 + k * 50
        for ot, cp in (("CE", "C"), ("PE", "P")):
            prem = g.bs_price(cp, 23395.0, strike, 7 / 365.0, 0.065, 0.13)
            gr = g.bs_greeks(cp, 23395.0, strike, 7 / 365.0, 0.065, 0.13)
            snap.rows[(strike, ot)] = OptionRow(security_id=f"S{strike}{ot}",
                symbol=f"NIFTY {strike} {ot}", underlying="NIFTY", expiry="2026-08-21",
                strike=strike, option_type=ot, ltp=round(prem, 2), oi=90000.0,
                volume=6000.0, iv=0.13, delta=gr["delta"], theta=gr["theta"])

    from datetime import datetime, timedelta, timezone
    IST = timezone(timedelta(hours=5, minutes=30))
    ctx_ts = int(datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0).timestamp())
    cfg = {"enabled": True, "rsi_max_bull": 45, "rsi_min_bear": 55, "iv_max_percentile": 70}
    strat = CandlestickStrategy({}, cfg)
    ctx = StrategyContext(
        underlying="NIFTY", spot=23395.0, ts=ctx_ts, chain=snap,
        iv_percentile=40.0, series_5m=_series_with_hammer_after_downtrend(),
        indicators={"rsi_1m": 32.0, "ema_fast_5m": 23450.0, "ema_slow_5m": 23500.0},
        config={"name": "NIFTY", "strike_interval": 50, "lot_size": 65},
    )
    sigs = strat.evaluate(ctx)
    assert sigs, "hammer after downtrend with low RSI should signal"
    assert sigs[0].option_type == "CE" and "hammer" in sigs[0].reason


if __name__ == "__main__":
    for fn in [test_single_patterns, test_two_bar_patterns, test_three_bar_patterns,
               test_analyze_series_detects_engulfing,
               test_candlestick_strategy_fires_on_hammer]:
        fn()
        print("ok " + fn.__name__)
    print("ALL PATTERN TESTS PASSED")
