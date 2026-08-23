"""Tests for the mean-reversion strategy and regime detection."""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.analytics.regime import REGIME_STRATEGY_MAP, detect_regime  # noqa: E402
from src.analytics import greeks as g  # noqa: E402
from src.broker.base import ChainSnapshot, OptionRow  # noqa: E402
from src.market.candles import Candle, CandleSeries  # noqa: E402
from src.strategies.base import StrategyContext  # noqa: E402
from src.strategies.meanrev import MeanReversionStrategy  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def _chain(spot):
    snap = ChainSnapshot(underlying="NIFTY", expiry="2026-08-27", spot=spot, ts=int(time.time()))
    atm = round(spot / 50) * 50
    for k in range(-5, 6):
        strike = atm + k * 50
        for ot, cp in (("CE", "C"), ("PE", "P")):
            prem = g.bs_price(cp, spot, strike, 5 / 365.0, 0.065, 0.13)
            snap.rows[(strike, ot)] = OptionRow(security_id=f"S{strike}{ot}",
                symbol=f"{strike} {ot}", underlying="NIFTY", expiry="2026-08-27",
                strike=strike, option_type=ot, ltp=round(prem, 2), oi=90000.0, volume=6000.0,
                iv=0.13, delta=0.5)
    return snap


def _ctx(spot, closes, rsi):
    s = CandleSeries(interval_sec=60)
    base = int(time.time()) - len(closes) * 60
    for i, c in enumerate(closes):
        s.candles.append(Candle(ts=base + i * 60, open=c, high=c * 1.001, low=c * 0.999, close=c))
    return StrategyContext(
        underlying="NIFTY", spot=spot,
        ts=int(datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0).timestamp()),
        chain=_chain(spot), iv_percentile=40.0, series_1m=s,
        indicators={"rsi_1m": rsi}, config={"name": "NIFTY", "strike_interval": 50, "lot_size": 65},
    )


def test_meanrev_overbought_buys_put():
    # strong uptrend spikes -> RSI 78, price above upper band
    closes = [24000 + i * 5 for i in range(40)]
    spot = closes[-1] * 1.002           # above upper band
    sigs = MeanReversionStrategy({}, {"enabled": True}).evaluate(_ctx(spot, closes, 78.0))
    assert sigs and sigs[0].option_type == "PE", sigs


def test_meanrev_oversold_buys_call():
    closes = [24000 - i * 5 for i in range(40)]
    spot = closes[-1] * 0.998           # below lower band
    sigs = MeanReversionStrategy({}, {"enabled": True}).evaluate(_ctx(spot, closes, 22.0))
    assert sigs and sigs[0].option_type == "CE", sigs


def test_regime_detection():
    assert detect_regime([], [], [], 25.0, 100.0, 99.0, 0.2, 0.2) == "TREND_UP"
    assert detect_regime([], [], [], 25.0, 100.0, 101.0, 0.2, 0.2) == "TREND_DOWN"
    assert detect_regime([], [], [], 12.0, 100.0, 99.5, 0.2, 0.2) == "RANGE"
    assert detect_regime([], [], [], 25.0, 100.0, 99.0, 0.6, 0.2) == "VOLATILE"
    assert "meanrev" in REGIME_STRATEGY_MAP and REGIME_STRATEGY_MAP["meanrev"] == {"RANGE"}


if __name__ == "__main__":
    test_meanrev_overbought_buys_put()
    print("ok test_meanrev_overbought_buys_put")
    test_meanrev_oversold_buys_call()
    print("ok test_meanrev_oversold_buys_call")
    test_regime_detection()
    print("ok test_regime_detection")
    print("ALL MEANREV/REGIME TESTS PASSED")
