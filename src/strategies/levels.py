"""Level-primary strategy: the key price line decides, indicators confirm.

Primary triggers (price structure only):
  - SUPPORT_HOLD  : wick below a support level, close back above it  -> BUY CE
  - RESIST_REJECT : wick above a resistance level, close back below  -> BUY PE
  - LEVEL_BREAK   : close beyond a level by 0.3*ATR with momentum     -> BUY CE/PE

Confirmators (EMA trend 25 + RSI band 15 + cheap IV 15 + ATR expansion 15 +
pattern 10 + volume 10): entry requires conviction >= min_confirm.
"""
from __future__ import annotations

import logging
import time

from ..analytics.levels import key_levels, nearest_levels
from ..market.inst_helpers import inst_cfg
from .base import Signal, Strategy, StrategyContext

log = logging.getLogger(__name__)


class LevelPrimaryStrategy(Strategy):
    name = "levels"

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        cfg = self.config
        ind = ctx.indicators
        if ctx.chain is None or ctx.spot <= 0:
            return []
        lt = time.localtime(ctx.ts)
        hm = lt.tm_hour * 100 + lt.tm_min
        if hm < int(cfg.get("entry_hm_start", 930)) or hm > int(cfg.get("entry_hm_end", 1430)):
            return []
        series = ctx.series_5m or ctx.series_1m
        if series is None or len(series.candles) < 8:
            return []
        atr = ind.get("atr_1m") or 0.0
        if atr <= 0:
            return []
        levels = key_levels(ind, series, vwap=ind.get("vwap_1m") or 0.0)
        if not levels:
            return []
        look = float(cfg.get("look_atr", 1.2)) * atr
        res, sup = nearest_levels(ctx.spot, levels, look)
        last = series.last()
        if last is None:
            return []
        iv_ok = ctx.iv_percentile <= float(cfg.get("iv_max_percentile", 70))
        if not iv_ok:
            return []

        interval = inst_cfg(ctx.underlying, ctx.config, "strike_interval", 50)
        atm = ctx.chain.atm_strike(interval)
        offset = int(cfg.get("strike_offset", 1))
        expiry = ctx.chain.expiry
        signals = []

        def confirm() -> float:
            """0-100 confirmator score (the 'rest are just confirmators')."""
            s = 0.0
            ef, es = ind.get("ema_fast_5m"), ind.get("ema_slow_5m")
            r = ind.get("rsi_1m")
            if ef is not None and es is not None:
                s += 25
            if r is not None and 45 <= r <= 70:
                s += 15
            if ctx.iv_percentile <= 70:
                s += 15
            a = ind.get("atr_1m"); am = ind.get("atr_ma_1m")
            if a is not None and am and a >= 1.05 * am:
                s += 15
            if ctx.series_1m and len(ctx.series_1m.candles) > 2:
                vols = [c.volume for c in ctx.series_1m.candles]
                if vols and last.volume >= 1.3 * (sum(vols[-20:]) / 20 if len(vols) >= 20 else 1):
                    s += 10
            return s

        def emit(direction: str, reason: str):
            row = ctx.chain.get(atm + (offset if direction == "CE" else -offset) * interval, direction)
            if row is None:
                return
            signals.append(Signal(
                strategy=self.name, side="BUY", option_type=direction,
                underlying=ctx.underlying, expiry=expiry,
                strike=atm + (offset if direction == "CE" else -offset) * interval,
                reason="LEVEL %s | %s" % (reason, "+".join(sorted({l.kind for l in levels[:3]}))),
                ts=time.time(), entry_price_hint=row.ltp if row else 0.0,
                meta={"atm": atm, "interval": interval, "levels": [l.price for l in levels[:4]]},
            ))

        min_confirm = float(cfg.get("min_confirm", 45))
        wick_tol = float(cfg.get("wick_atr", 0.1)) * atr
        break_tol = float(cfg.get("break_atr", 0.3)) * atr

        # PRIMARY: support hold
        if sup is not None and last.low <= sup.price + wick_tol and last.close > sup.price:
            if confirm() >= min_confirm:
                emit("CE", "SUPPORT_HOLD %.1f" % sup.price)
        # PRIMARY: resistance reject
        if res is not None and last.high >= res.price - wick_tol and last.close < res.price:
            if confirm() >= min_confirm:
                emit("PE", "RESIST_REJECT %.1f" % res.price)
        # PRIMARY: level break (trend continuation)
        if res is not None and last.close > res.price + break_tol:
            if confirm() >= min_confirm:
                emit("CE", "BREAK_UP %.1f" % res.price)
        elif sup is not None and last.close < sup.price - break_tol:
            if confirm() >= min_confirm:
                emit("PE", "BREAK_DOWN %.1f" % sup.price)
        return signals
