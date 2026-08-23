"""Stock BTST screener: picks liquid, trend-strong stocks for buy-today-sell-tomorrow.

Screens on DAILY data (60 days) with classic BTST filters:
  - liquidity: price >= min_price, 20d avg volume >= min_avg_volume
  - trend    : close > EMA20 > EMA50
  - momentum : RSI(14) in [rsi_min, rsi_max], ROC(5) > 0
  - volume   : today's volume >= vol_mult x 20d average (accumulation)
  - strength : close within (1 - strength_min) of the 60-day high

Picks are scored and ranked; the top N become BTST candidates for next-day entry.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from ..analytics import indicators as ta

log = logging.getLogger(__name__)

# Default liquid NIFTY-50 universe (extend via data/stock_universe.csv)
DEFAULT_UNIVERSE = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC",
    "LT", "KOTAKBANK", "AXISBANK", "MARUTI", "TITAN", "ASIANPAINT", "HCLTECH",
    "WIPRO", "ULTRACEMCO", "SUNPHARMA", "BAJFINANCE", "NTPC", "POWERGRID", "ONGC",
    "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "HINDUNILVR", "BAJAJFINSV", "ADANIPORTS",
    "NESTLEIND", "TECHM", "COALINDIA", "GRASIM", "DRREDDY", "CIPLA", "APOLLOHOSP",
    "EICHERMOT", "HEROMOTOCO", "HINDALCO", "BRITANNIA", "DIVISLAB", "SBILIFE",
    "INDUSINDBK", "BAJAJ-AUTO", "TATACONSUM", "HDFCLIFE", "UPL", "ADANIENT", "LTIM",
    "M&M", "SHRIRAMFIN",
]


@dataclass
class StockPick:
    symbol: str
    security_id: str
    close: float
    score: float
    reason: str
    ema20: float = 0.0
    ema50: float = 0.0
    rsi: float = 0.0
    vol_ratio: float = 0.0
    strength: float = 0.0            # close / 60-day high
    meta: dict = field(default_factory=dict)


class StockScreener:
    def __init__(self, data, master, cfg: dict):
        self.data = data
        self.master = master
        self.cfg = cfg.get("stock_btst", {})

    # ------------------------------------------------------------------
    def universe(self) -> dict[str, str]:
        """symbol -> security_id. Uses config list / data/stock_universe.csv."""
        symbols = self.cfg.get("universe") or DEFAULT_UNIVERSE
        try:
            import csv
            from pathlib import Path
            p = Path(__file__).resolve().parent.parent.parent / "data" / "stock_universe.csv"
            if p.exists():
                with open(p, newline="", encoding="utf-8") as f:
                    rows = list(csv.reader(f))
                extra = [r[0].strip().upper() for r in rows if r and r[0].strip()]
                if extra:
                    symbols = extra
        except Exception as e:
            log.warning("universe csv failed: %s", e)
        out = {}
        for sym in symbols:
            sym = sym.strip().upper()
            sid = self.master.stock_security_id(sym)
            if sid:
                out[sym] = sid
            else:
                log.info("no scrip-master entry for %s - skipped", sym)
        return out

    # ------------------------------------------------------------------
    def screen(self, days: int = 60, live: dict | None = None) -> list[StockPick]:
        """Screen for BTST candidates.

        live: {security_id: {"ltp", "open", "high", "low", "volume"}} - today's
        live bar (screened 15:00-15:20 while the market is still open). Without
        it, the last history row is treated as today (volume surge check skipped)."""
        u = self.cfg
        min_price = float(u.get("min_price", 50))
        min_avg_vol = float(u.get("min_avg_volume", 200000))
        vol_mult = float(u.get("vol_mult", 1.5))
        rsi_min, rsi_max = float(u.get("rsi_min", 52)), float(u.get("rsi_max", 72))
        strength_min = float(u.get("strength_min", 0.92))
        max_n = int(u.get("max_positions", 1))

        today = date.today()
        from_d = (today - timedelta(days=days + 10)).isoformat()
        to_d = (today + timedelta(days=1)).isoformat()
        picks = []
        for sym, sid in self.universe().items():
            try:
                rows = self.data.historical_daily(sid, "NSE_EQ", "EQUITY", from_d, to_d)
            except Exception as e:
                log.warning("daily data %s failed: %s", sym, e)
                continue
            if len(rows) < 40:
                continue
            rows = sorted(rows, key=lambda r: r["timestamp"])
            # append today's live (still-forming) bar when available
            lv = (live or {}).get(sid) or {}
            if lv.get("ltp"):
                rows = rows + [{
                    "timestamp": rows[-1]["timestamp"] + 86400,
                    "open": lv.get("open") or float(rows[-1]["close"]),
                    "high": lv.get("high") or float(rows[-1]["close"]),
                    "low": lv.get("low") or float(rows[-1]["close"]),
                    "close": lv["ltp"],
                    "volume": lv.get("volume") or float(rows[-1].get("volume", 0) or 0),
                }]
            closes = [float(r["close"]) for r in rows]
            highs = [float(r["high"]) for r in rows]
            vols = [float(r.get("volume", 0) or 0) for r in rows]
            price = closes[-1]
            if price < min_price or price <= 0:
                continue
            avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else 0
            if avg_vol < min_avg_vol:
                continue
            e20 = ta.ema(closes, 20)[-1]
            e50 = ta.ema(closes, 50)[-1]
            rsi = ta.rsi(closes, 14)[-1]
            vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 0.0
            vol_known = bool(lv.get("volume"))
            high60 = max(highs[-min(60, len(highs)):])
            strength = price / high60 if high60 > 0 else 0.0
            roc5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 and closes[-6] > 0 else 0.0

            trend = price > e20 > e50
            rsi_ok = rsi_min <= rsi <= rsi_max
            vol_ok = (vol_ratio >= vol_mult) if vol_known else True
            strength_ok = strength >= strength_min
            mom_ok = roc5 > 0

            if not (trend and rsi_ok and vol_ok and strength_ok and mom_ok):
                continue
            # score: RSI near 60 + volume surge + trend strength + 52w proximity
            score = (10 - abs(rsi - 60)) / 10.0
            score += min(vol_ratio / 3.0, 1.0) * 0.4
            score += (strength - strength_min) * 3.0
            if e20 > e50:
                score += 0.2
            picks.append(StockPick(
                symbol=sym, security_id=sid, close=price, score=round(score, 3),
                reason="trend+volume BTST", ema20=e20, ema50=e50, rsi=rsi,
                vol_ratio=round(vol_ratio, 2), strength=round(strength, 3),
                meta={"roc5": round(roc5, 2), "high60": high60},
            ))
        picks.sort(key=lambda p: p.score, reverse=True)
        return picks[:max_n]
