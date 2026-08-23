"""Technical indicators for price-action signals.

Pure numpy functions over price arrays (list-like accepted).
Return numpy arrays; warm-up values are NaN (never trade on warm-up bars).
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


def _arr(x: Sequence[float]) -> np.ndarray:
    return np.asarray(x, dtype=float)


def sma(x: Sequence[float], period: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if len(x) < period:
        return out
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[period - 1:] = (c[period:] - c[:-period]) / period
    return out


def ema(x: Sequence[float], period: int) -> np.ndarray:
    """EMA seeded with SMA of the first 'period' values."""
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if len(x) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = float(np.mean(x[:period]))
    for i in range(period, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(closes: Sequence[float], period: int = 14) -> np.ndarray:
    """Wilder's RSI."""
    c = _arr(closes)
    out = np.full(len(c), np.nan)
    if len(c) <= period:
        return out
    deltas = np.diff(c)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, len(c)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> np.ndarray:
    h, l, c = _arr(high), _arr(low), _arr(close)
    out = np.full(len(c), np.nan)
    if len(c) < 2:
        return out
    out[0] = h[0] - l[0]
    out[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return out


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int = 14) -> np.ndarray:
    """Wilder's ATR."""
    tr = true_range(high, low, close)
    out = np.full(len(tr), np.nan)
    valid = ~np.isnan(tr)
    if valid.sum() < period:
        return out
    first = int(np.argmax(valid))
    out[first + period - 1] = float(np.mean(tr[first:first + period]))
    for i in range(first + period, len(tr)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def adx(high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int = 14) -> np.ndarray:
    """Wilder's ADX (trend strength), 0..100."""
    h, l, c = _arr(high), _arr(low), _arr(close)
    out = np.full(len(c), np.nan)
    if len(c) <= period + 1:
        return out
    up = h[1:] - h[:-1]
    dn = l[:-1] - l[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(h, l, c)[1:]

    def wilder(series: np.ndarray, n: int) -> np.ndarray:
        out_s = np.full(len(series), np.nan)
        out_s[n - 1] = float(np.sum(series[:n]))
        for i in range(n, len(series)):
            out_s[i] = out_s[i - 1] - out_s[i - 1] / n + series[i]
        return out_s

    atr_s = wilder(tr, period)
    plus_s = wilder(plus_dm, period)
    minus_s = wilder(minus_dm, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100.0 * plus_s / atr_s
        mdi = 100.0 * minus_s / atr_s
        dx = 100.0 * np.abs(pdi - mdi) / (pdi + mdi)
    start = period * 2 - 1
    if start < len(dx):
        out[start] = float(np.nanmean(dx[: start + 1]))
        for i in range(start, len(dx)):
            out[i + 1] = (out[i] * (period - 1) + dx[i]) / period
    return out


def vwap(typical: Sequence[float], volume: Sequence[float]) -> np.ndarray:
    """Running cumulative VWAP."""
    t, v = _arr(typical), _arr(volume)
    if len(t) == 0:
        return np.array([])
    pv = np.cumsum(t * v)
    vv = np.cumsum(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = pv / vv
    out[vv == 0] = np.nan
    return out


def rolling_std(x: Sequence[float], period: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if len(x) < period:
        return out
    for i in range(period - 1, len(x)):
        out[i] = float(np.std(x[i - period + 1: i + 1]))
    return out


def rolling_max(x: Sequence[float], period: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if len(x) == 0:
        return out
    for i in range(len(x)):
        lo = max(0, i - period + 1)
        out[i] = float(np.max(x[lo: i + 1]))
    return out


def rolling_min(x: Sequence[float], period: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if len(x) == 0:
        return out
    for i in range(len(x)):
        lo = max(0, i - period + 1)
        out[i] = float(np.min(x[lo: i + 1]))
    return out


def linear_slope(y: Sequence[float], period: int) -> np.ndarray:
    """Slope of least-squares line over trailing window (points per bar)."""
    y = _arr(y)
    out = np.full(len(y), np.nan)
    if len(y) < period:
        return out
    xs = np.arange(period, dtype=float)
    x_mean = xs.mean()
    denom = float(((xs - x_mean) ** 2).sum())
    for i in range(period - 1, len(y)):
        ys = y[i - period + 1: i + 1]
        if np.any(np.isnan(ys)):
            continue
        out[i] = float(((xs - x_mean) * (ys - ys.mean())).sum() / denom)
    return out


def swing_pivots(high: Sequence[float], low: Sequence[float], left: int = 2, right: int = 2):
    """Return (pivot_highs, pivot_lows): price where a strict pivot is confirmed else NaN."""
    h, l = _arr(high), _arr(low)
    ph = np.full(len(h), np.nan)
    pl = np.full(len(l), np.nan)
    for i in range(left, len(h) - right):
        seg_h = h[i - left: i + right + 1]
        seg_l = l[i - left: i + right + 1]
        before_h = seg_h[:left]
        after_h = seg_h[left + 1:]
        before_l = seg_l[:left]
        after_l = seg_l[left + 1:]
        if h[i] > np.max(np.concatenate([before_h, after_h])):
            ph[i] = h[i]
        if l[i] < np.min(np.concatenate([before_l, after_l])):
            pl[i] = l[i]
    return ph, pl
