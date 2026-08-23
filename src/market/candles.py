"""Candle builder: aggregate live ticks into 1m/5m candles and seed from history."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass
class Candle:
    ts: int            # epoch seconds of candle open
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: float = 0.0

    @property
    def is_complete(self) -> bool:
        return time.time() >= self.ts + 60  # 1m candle


@dataclass
class CandleSeries:
    interval_sec: int = 60
    candles: deque = field(default_factory=lambda: deque(maxlen=500))

    def seed(self, rows: Iterable[dict]):
        """Seed from historical rows: {'timestamp': epoch, 'open','high','low','close','volume'}."""
        for r in sorted(rows, key=lambda x: x["timestamp"]):
            ts = int(r["timestamp"])
            self.candles.append(Candle(
                ts=ts - (ts % self.interval_sec),
                open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]),
                volume=float(r.get("volume", 0.0)), oi=float(r.get("open_interest", 0.0)),
            ))

    def last(self) -> Optional[Candle]:
        return self.candles[-1] if self.candles else None

    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    def highs(self) -> list[float]:
        return [c.high for c in self.candles]

    def lows(self) -> list[float]:
        return [c.low for c in self.candles]

    def append_tick(self, ts: int, price: float, volume: float = 0.0, oi: float = 0.0) -> Optional[Candle]:
        """Feed a tick; returns the just-completed candle when a new one starts."""
        bucket = ts - (ts % self.interval_sec)
        completed = None
        last = self.last()
        if last is None or last.ts < bucket:
            if last is not None:
                completed = last
            self.candles.append(Candle(ts=bucket, open=price, high=price, low=price, close=price,
                                       volume=volume, oi=oi))
        else:
            last.high = max(last.high, price)
            last.low = min(last.low, price)
            last.close = price
            last.volume += volume
            last.oi = oi if oi else last.oi
        return completed


def resample(candles: Iterable[Candle], new_interval_min: int) -> list[Candle]:
    """Resample 1m candles into larger buckets."""
    out: list[Candle] = []
    bucket_sec = new_interval_min * 60
    current: Optional[Candle] = None
    for c in sorted(candles, key=lambda x: x.ts):
        b = c.ts - (c.ts % bucket_sec)
        if current is None or current.ts != b:
            if current is not None:
                out.append(current)
            current = Candle(ts=b, open=c.open, high=c.high, low=c.low, close=c.close,
                             volume=c.volume, oi=c.oi)
        else:
            current.high = max(current.high, c.high)
            current.low = min(current.low, c.low)
            current.close = c.close
            current.volume += c.volume
            current.oi = c.oi
    if current is not None:
        out.append(current)
    return out
