"""Trend + IV-filter momentum: buy ATM/OTM options when a pullback in a fresh
trend completes, premium is cheap (IV percentile low), and ATR is expanding."""
from __future__ import annotations

import logging
import time

from ..broker.base import ChainSnapshot
from ..market.inst_helpers import inst_cfg
from .base import Signal, Strategy, StrategyContext

log = logging.getLogger(__name__)


class TrendMomentumStrategy(Strategy):
    name = "momentum"

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        cfg = self.config
        ind = ctx.indicators
        ef, es = ind.get("ema_fast_1m"), ind.get("ema_slow_1m")
        ef5, es5 = ind.get("ema_fast_5m"), ind.get("ema_slow_5m")
        r = ind.get("rsi_1m")
        atr = ind.get("atr_1m")
        vwap = ind.get("vwap_1m")
        atr_ma = ind.get("atr_ma_1m")
        if any(v is None for v in (ef, es, ef5, es5, r, atr, vwap, atr_ma)):
            return []
        if ctx.chain is None or ctx.spot <= 0:
            return []

        # entry time window: avoid late-day chop
        lt = time.localtime(ctx.ts)
        hm = lt.tm_hour * 100 + lt.tm_min
        if hm < int(cfg.get("entry_hm_start", 925)) or hm > int(cfg.get("entry_hm_end", 1330)):
            return []

        interval = inst_cfg(ctx.underlying, ctx.config, "strike_interval", 50)
        atm = ctx.chain.atm_strike(interval)
        offset = int(cfg.get("strike_offset", 1))
        pullback_mult = float(cfg.get("atr_pullback_mult", 0.5))
        rsi_min, rsi_max = float(cfg.get("rsi_min", 52)), float(cfg.get("rsi_max", 80))
        iv_max = float(cfg.get("iv_max_percentile", 70))
        atr_expand = float(cfg.get("min_atr_expansion", 1.05))
        last = ctx.series_1m.last() if ctx.series_1m else None
        if last is None:
            return []

        trend_up = ef > es and ef5 > es5 and ctx.spot > vwap
        trend_dn = ef < es and ef5 < es5 and ctx.spot < vwap
        # price has touched the EMA buy/sell zone (pullback or fresh cross); avoids chasing
        zone_up = last.low <= es + pullback_mult * atr
        zone_dn = last.high >= es - pullback_mult * atr
        pullback_up = zone_up and last.close >= es - 0.25 * atr
        pullback_dn = zone_dn and last.close <= es + 0.25 * atr
        atr_ok = atr >= atr_expand * atr_ma
        iv_ok = ctx.iv_percentile <= iv_max
        rsi_up_ok = rsi_min <= r <= rsi_max
        rsi_dn_ok = (100 - rsi_max) <= r <= (100 - rsi_min)

        signals = []
        expiry = ctx.chain.expiry
        if trend_up and pullback_up and atr_ok and iv_ok and rsi_up_ok:
            strike = atm + offset * interval
            row = ctx.chain.get(strike, "CE")
            signals.append(Signal(
                strategy=self.name, side="BUY", option_type="CE",
                underlying=ctx.underlying, expiry=expiry, strike=strike,
                reason="bullish trend + pullback, IV pct=%.0f, RSI=%.1f" % (ctx.iv_percentile, r),
                ts=time.time(), entry_price_hint=row.ltp if row else 0.0,
                meta={"atm": atm, "interval": interval, "max_hold_min": int(cfg.get("max_hold_min", 150))},
            ))
        elif trend_dn and pullback_dn and atr_ok and iv_ok and rsi_dn_ok:
            strike = atm - offset * interval
            row = ctx.chain.get(strike, "PE")
            signals.append(Signal(
                strategy=self.name, side="BUY", option_type="PE",
                underlying=ctx.underlying, expiry=expiry, strike=strike,
                reason="bearish trend + pullback, IV pct=%.0f, RSI=%.1f" % (ctx.iv_percentile, r),
                ts=time.time(), entry_price_hint=row.ltp if row else 0.0,
                meta={"atm": atm, "interval": interval, "max_hold_min": int(cfg.get("max_hold_min", 150))},
            ))
        return signals
