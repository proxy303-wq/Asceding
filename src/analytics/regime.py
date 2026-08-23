"""Market regime detection (StratFusion-style dynamic strategy selection).

Regimes: TREND_UP / TREND_DOWN / RANGE / VOLATILE.
  - ADX(14) >= 20 with EMA50 slope up  -> TREND_UP (and -dn -> TREND_DOWN)
  - ADX < 20 and ATR% moderate         -> RANGE
  - ATR% in the top decile of recent   -> VOLATILE (news/expansion)
"""
from __future__ import annotations

from typing import Optional


def detect_regime(closes: list[float], highs: list[float], lows: list[float],
                  adx_5m: Optional[float], ema_slow_5m: Optional[float],
                  ema_slow_prev_5m: Optional[float], atr_pct: float,
                  atr_pct_avg: float) -> str:
    if adx_5m is None:
        return "RANGE"
    # volatility expansion -> VOLATILE overrides trend/range
    if atr_pct_avg > 0 and atr_pct >= 1.5 * atr_pct_avg:
        return "VOLATILE"
    if adx_5m >= 20:
        if ema_slow_5m is not None and ema_slow_prev_5m is not None:
            if ema_slow_5m > ema_slow_prev_5m:
                return "TREND_UP"
            if ema_slow_5m < ema_slow_prev_5m:
                return "TREND_DOWN"
    return "RANGE"


# which strategies may fire in which regime (StratFusion dynamic selection)
REGIME_STRATEGY_MAP = {
    "momentum":    {"TREND_UP", "TREND_DOWN", "VOLATILE"},
    "breakout":    {"TREND_UP", "TREND_DOWN", "VOLATILE"},
    "candlestick": {"RANGE", "TREND_UP", "TREND_DOWN"},
    "levels":     {"RANGE", "TREND_UP", "TREND_DOWN"},
    "meanrev":     {"RANGE"},
    "contrarian":  {"RANGE"},
}
