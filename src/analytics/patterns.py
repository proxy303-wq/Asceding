"""Candlestick pattern detection (classic single/double/triple candle patterns).

Detection is deterministic: it only uses the last few candles of an OHLC series
and configurable tolerances. Context (trend/support) is applied by strategies,
not here - this module only names what a bar/candle *is*.
"""
from __future__ import annotations

from typing import Optional, Sequence

# ---------------------------------------------------------------- helpers


def _body(o, c):
    return abs(c - o)


def _range(h, l):
    return h - l


def _upper_wick(h, o, c):
    return h - max(o, c)


def _lower_wick(o, c, l):
    return min(o, c) - l


def _is_bull(o, c):
    return c > o


def _is_bear(o, c):
    return c < o


def _body_pct(o, c, h, l):
    r = _range(h, l)
    return _body(o, c) / r if r > 0 else 0.0


def _tol(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- single bar


def single_bar_pattern(o, h, l, c, cfg: dict) -> Optional[str]:
    """Name of the pattern formed by this single candle, or None."""
    body = _body(o, c)
    rng = _range(h, l)
    if rng <= 0:
        return None
    up = _upper_wick(h, o, c)
    lo = _lower_wick(o, c, l)
    doji_t = _tol(cfg, "doji_body_pct", 0.10) * rng
    if body <= doji_t:
        # long shadow on one side + tiny wick on the other = doji variants
        if lo >= 0.6 * rng and up <= 0.1 * rng:
            return "dragonfly_doji"
        if up >= 0.6 * rng and lo <= 0.1 * rng:
            return "gravestone_doji"
        return "doji"
    body_ratio = body / rng
    wick_t = _tol(cfg, "wick_mult", 2.0)
    if body_ratio >= _tol(cfg, "marubozu_body_pct", 0.90):
        return "marubozu"
    if lo >= wick_t * body and up <= 0.5 * body:
        return "hammer"
    if up >= wick_t * body and lo <= 0.5 * body:
        return "shooting_star"
    if _tol(cfg, "spinning_body_pct", 0.25) >= body_ratio:
        return "spinning_top"
    return None


# ---------------------------------------------------------------- two bars


def two_bar_pattern(o0, h0, l0, c0, o1, h1, l1, c1, cfg: dict) -> Optional[str]:
    """Pattern of the LAST two candles (0 = previous, 1 = current)."""
    body0, body1 = _body(o0, c0), _body(o1, c1)
    rng1 = _range(h1, l1)
    if rng1 <= 0 or body0 <= 0 or body1 <= 0:
        return None
    tol_px = _tol(cfg, "tweezer_tol_px", 0.0)

    # Engulfing
    if _is_bear(o0, c0) and _is_bull(o1, c1) and h1 >= h0 and l1 <= l0:
        return "bullish_engulfing"
    if _is_bull(o0, c0) and _is_bear(o1, c1) and h1 >= h0 and l1 <= l0:
        return "bearish_engulfing"

    # Harami (current body inside previous body)
    if _is_bear(o0, c0) and _is_bull(o1, c1) and o1 >= c0 and c1 <= o0 and body1 < 0.6 * body0:
        return "bullish_harami"
    if _is_bull(o0, c0) and _is_bear(o1, c1) and c1 >= c0 and o1 <= o0 and body1 < 0.6 * body0:
        return "bearish_harami"

    # Piercing line / dark cloud cover
    if _is_bear(o0, c0) and _is_bull(o1, c1) and o1 < l0 and c1 > (o0 + c0) / 2 and c1 < o0:
        return "piercing_line"
    if _is_bull(o0, c0) and _is_bear(o1, c1) and o1 > h0 and c1 < (o0 + c0) / 2 and c1 > o0:
        return "dark_cloud_cover"

    # Tweezers
    if _is_bull(o0, c0) and _is_bear(o1, c1) and abs(h0 - h1) <= max(tol_px, 0.02 * rng1):
        return "tweezer_top"
    if _is_bear(o0, c0) and _is_bull(o1, c1) and abs(l0 - l1) <= max(tol_px, 0.02 * rng1):
        return "tweezer_bottom"
    return None


# ---------------------------------------------------------------- three bars


def three_bar_pattern(o0, h0, l0, c0, o1, h1, l1, c1, o2, h2, l2, c2, cfg: dict) -> Optional[str]:
    """Pattern of the last three candles (0,1 = previous, 2 = current)."""
    # Morning / evening star
    if (_is_bear(o0, c0) and _body(o1, c1) <= 0.4 * _body(o0, c0)
            and _is_bull(o2, c2) and c2 > (o0 + c0) / 2):
        return "morning_star"
    if (_is_bull(o0, c0) and _body(o1, c1) <= 0.4 * _body(o0, c0)
            and _is_bear(o2, c2) and c2 < (o0 + c0) / 2):
        return "evening_star"
    # Three soldiers / crows
    if (_is_bull(o0, c0) and _is_bull(o1, c1) and _is_bull(o2, c2)
            and c0 < c1 < c2 and o0 < o1 < o2
            and all(_body_pct(o, c, h, l) >= 0.6 for o, c, h, l in ((o0, c0, h0, l0), (o1, c1, h1, l1), (o2, c2, h2, l2)))):
        return "three_white_soldiers"
    if (_is_bear(o0, c0) and _is_bear(o1, c1) and _is_bear(o2, c2)
            and c0 > c1 > c2 and o0 > o1 > o2
            and all(_body_pct(o, c, h, l) >= 0.6 for o, c, h, l in ((o0, c0, h0, l0), (o1, c1, h1, l1), (o2, c2, h2, l2)))):
        return "three_black_crows"
    return None


# ---------------------------------------------------------------- series API


def analyze_candles(opens: Sequence[float], highs: Sequence[float],
                    lows: Sequence[float], closes: Sequence[float],
                    cfg: dict | None = None) -> list[dict]:
    """Return [{index, pattern}] for the last three bars of the series."""
    cfg = cfg or {}
    out = []
    n = len(closes)
    if n < 3:
        return out
    for i in range(n - 3, n):
        pat = single_bar_pattern(opens[i], highs[i], lows[i], closes[i], cfg)
        if pat:
            out.append({"index": i, "pattern": pat, "bars": 1})
        if i >= 1:
            pat2 = two_bar_pattern(opens[i - 1], highs[i - 1], lows[i - 1], closes[i - 1],
                                   opens[i], highs[i], lows[i], closes[i], cfg)
            if pat2:
                out.append({"index": i, "pattern": pat2, "bars": 2})
        if i >= 2:
            pat3 = three_bar_pattern(opens[i - 2], highs[i - 2], lows[i - 2], closes[i - 2],
                                     opens[i - 1], highs[i - 1], lows[i - 1], closes[i - 1],
                                     opens[i], highs[i], lows[i], closes[i], cfg)
            if pat3:
                out.append({"index": i, "pattern": pat3, "bars": 3})
    return out


BULLISH_PATTERNS = {
    "doji", "dragonfly_doji", "hammer", "inverted_hammer", "bullish_engulfing",
    "bullish_harami", "piercing_line", "tweezer_bottom", "morning_star",
    "three_white_soldiers", "spinning_top",
}
BEARISH_PATTERNS = {
    "doji", "gravestone_doji", "shooting_star", "hanging_man", "bearish_engulfing",
    "bearish_harami", "dark_cloud_cover", "tweezer_top", "evening_star",
    "three_black_crows", "spinning_top",
}
