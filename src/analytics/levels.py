"""Key price levels (the PRIMARY signal source).

Support/resistance derived from structure: prior-day high/low, opening range,
swing pivots (5m), VWAP and round strikes. Indicators confirm; levels decide.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..analytics import indicators as ta


@dataclass
class PriceLevel:
    price: float
    kind: str          # PD_HIGH / PD_LOW / OR_HIGH / OR_LOW / PIVOT_HIGH / PIVOT_LOW / VWAP
    strength: float    # 0..1 how many sources agree


def key_levels(ind: dict, series_5m=None, vwap: float = 0.0) -> list[PriceLevel]:
    """Collect candidate levels with a strength score (more sources = stronger)."""
    levels: list[PriceLevel] = []
    votes: dict[float, set] = {}

    def add(p: float, kind: str):
        if not p or p != p or p <= 0:
            return
        votes.setdefault(round(p, 1), set()).add(kind)

    if ind.get("pd_high"): add(ind["pd_high"], "PD_HIGH")
    if ind.get("pd_low"): add(ind["pd_low"], "PD_LOW")
    if ind.get("or_high"): add(ind["or_high"], "OR_HIGH")
    if ind.get("or_low"): add(ind["or_low"], "OR_LOW")
    if vwap and vwap > 0: add(vwap, "VWAP")
    if series_5m is not None and len(series_5m.candles) > 8:
        ph, pl = ta.swing_pivots(series_5m.highs(), series_5m.lows(), left=2, right=2)
        for p in ph[-6:]:
            if p == p:
                add(p, "PIVOT_HIGH")
        for p in pl[-6:]:
            if p == p:
                add(p, "PIVOT_LOW")

    for price, kinds in votes.items():
        levels.append(PriceLevel(price=price, kind="+".join(sorted(kinds)),
                                 strength=min(1.0, len(kinds) / 2.0)))
    levels.sort(key=lambda l: l.price)
    return levels


def nearest_levels(price: float, levels: list[PriceLevel], look: float) -> tuple[Optional[PriceLevel], Optional[PriceLevel]]:
    """(resistance above, support below) within 'look' of price."""
    above = [l for l in levels if l.price > price + 1e-9 and l.price - price <= look]
    below = [l for l in levels if l.price < price - 1e-9 and price - l.price <= look]
    res = min(above, key=lambda l: l.price - price) if above else None
    sup = min(below, key=lambda l: price - l.price) if below else None
    return res, sup
