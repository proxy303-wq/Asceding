"""ML feature extraction at signal time.

Follows the research direction (LSTM-style sequence lookback is embedded as
rolling window features) but stays tabular so gradient-boosted trees - the
practical best for this binary win/loss gate - can consume it directly.
"""
from __future__ import annotations

import math
from typing import Optional

FEATURE_NAMES = [
    "rsi_1m", "ema_gap_1m", "ema_gap_5m", "atr_pct", "vwap_ratio",
    "iv_pct", "trend_score", "hour", "dte", "expected_move_pct",
    "delta", "theta_pct", "or_distance_pct", "dow", "ret_5", "ret_15",
]


def extract_features(st, sig, d) -> list[float]:
    """Build the feature vector for a signal. Returns list aligned to FEATURE_NAMES."""
    ind = st.indicators
    ef, es = ind.get("ema_fast_1m"), ind.get("ema_slow_1m")
    ef5, es5 = ind.get("ema_fast_5m"), ind.get("ema_slow_5m")
    r = ind.get("rsi_1m")
    atr = ind.get("atr_1m") or 0.0
    vwap = ind.get("vwap_1m")
    spot = st.spot or 0.0

    ema_gap_1m = (ef - es) / atr if (ef is not None and es is not None and atr > 0) else 0.0
    ema_gap_5m = (ef5 - es5) / atr if (ef5 is not None and es5 is not None and atr > 0) else 0.0
    atr_pct = atr / spot * 100.0 if spot > 0 else 0.0
    vwap_ratio = (spot - vwap) / atr if (vwap and atr > 0) else 0.0

    # trend score: fraction of last 10 closes moving in the signal direction
    trend_score = 0.0
    closes = st.series_1m.closes() if st.series_1m else []
    if len(closes) > 10:
        ups = sum(1 for i in range(-10, 0) if closes[i] > closes[i - 1])
        if sig.option_type == "CE":
            trend_score = ups / 10.0
        else:
            trend_score = (10 - ups) / 10.0

    # rolling returns (LSTM-style lookback flavor)
    ret_5 = math.log(closes[-1] / closes[-6]) if len(closes) >= 6 and closes[-6] > 0 else 0.0
    ret_15 = math.log(closes[-1] / closes[-16]) if len(closes) >= 16 and closes[-16] > 0 else 0.0

    # distance from the opening range (as % of spot)
    or_h, or_l = ind.get("or_high"), ind.get("or_low")
    or_dist = 0.0
    if or_h and or_l and spot > 0:
        mid = (or_h + or_l) / 2.0
        or_dist = (spot - mid) / spot * 100.0

    greeks = sig.meta.get("greeks", {})
    theta_pct = 0.0
    if greeks.get("theta"):
        premium = sig.entry_price_hint or greeks.get("iv", 0.01)
        if premium > 0:
            theta_pct = -greeks["theta"] / premium * 100.0

    hour = 0
    try:
        from datetime import datetime
        hour = datetime.fromtimestamp(st.ts).hour
    except Exception:
        pass

    return [
        float(r or 0.0) if r is not None else 0.0,
        ema_gap_1m, ema_gap_5m, atr_pct, vwap_ratio,
        st.iv_percentile, trend_score, float(hour), float(d),
        greeks.get("expected_move", 0.0) / spot * 100.0 if spot > 0 else 0.0,
        float(greeks.get("delta", 0.0)), theta_pct, or_dist,
        float(getattr(st, "dow", 0.0)), ret_5, ret_15,
    ]


def feature_dict(v: list[float]) -> dict:
    return dict(zip(FEATURE_NAMES, v))
