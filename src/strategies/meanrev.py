"""Mean-reversion (StratFusion: RSI + Bollinger Bands) for range regimes.

Buys PUTs when RSI > overbought AND price above the upper Bollinger band
(overextension -> snap back), buys CALLs when RSI < oversold AND price below
the lower band. Only effective in RANGE regimes (enforced by the fusion gate).
"""
from __future__ import annotations

import logging
import time

from ..analytics import indicators as ta
from ..market.inst_helpers import inst_cfg
from .base import Signal, Strategy, StrategyContext

log = logging.getLogger(__name__)


class MeanReversionStrategy(Strategy):
    name = "meanrev"

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        cfg = self.config
        ind = ctx.indicators
        if ctx.chain is None or ctx.spot <= 0:
            return []
        lt = time.localtime(ctx.ts)
        hm = lt.tm_hour * 100 + lt.tm_min
        if hm < int(cfg.get("entry_hm_start", 1000)) or hm > int(cfg.get("entry_hm_end", 1430)):
            return []
        series = ctx.series_1m or ctx.series_5m
        if series is None or len(series.candles) < 30:
            return []
        closes = series.closes()
        r = ind.get("rsi_1m")
        if r is None:
            return []
        # Bollinger bands (20, 2.0) on the working series
        bb_period = int(cfg.get("bb_period", 20))
        bb_mult = float(cfg.get("bb_mult", 2.0))
        mid = ta.sma(closes, bb_period)[-1]
        sd = ta.rolling_std(closes, bb_period)[-1]
        if mid != mid or sd != sd or sd <= 0:
            return []
        upper, lower = mid + bb_mult * sd, mid - bb_mult * sd
        if ctx.iv_percentile > float(cfg.get("iv_max_percentile", 70)):
            return []

        interval = inst_cfg(ctx.underlying, ctx.config, "strike_interval", 50)
        atm = ctx.chain.atm_strike(interval)
        offset = int(cfg.get("strike_offset", 1))
        expiry = ctx.chain.expiry
        rsi_ob = float(cfg.get("rsi_overbought", 72))
        rsi_os = float(cfg.get("rsi_oversold", 28))

        signals = []
        if r >= rsi_ob and ctx.spot > upper:
            strike = atm - offset * interval
            row = ctx.chain.get(strike, "PE")
            signals.append(Signal(
                strategy=self.name, side="BUY", option_type="PE",
                underlying=ctx.underlying, expiry=expiry, strike=strike,
                reason="meanrev: RSI %.0f > upper BB (overbought) - buy PUT" % r,
                ts=time.time(), entry_price_hint=row.ltp if row else 0.0,
                meta={"atm": atm, "interval": interval, "max_hold_min": int(cfg.get("max_hold_min", 150))},
            ))
        elif r <= rsi_os and ctx.spot < lower:
            strike = atm + offset * interval
            row = ctx.chain.get(strike, "CE")
            signals.append(Signal(
                strategy=self.name, side="BUY", option_type="CE",
                underlying=ctx.underlying, expiry=expiry, strike=strike,
                reason="meanrev: RSI %.0f < lower BB (oversold) - buy CE" % r,
                ts=time.time(), entry_price_hint=row.ltp if row else 0.0,
                meta={"atm": atm, "interval": interval, "max_hold_min": int(cfg.get("max_hold_min", 150))},
            ))
        return signals
