"""IV-contrarian premium selling (short strangle) — OFF by default.

Sells an OTM strangle when IV is rich (high percentile) AND the market is
range-bound (low ADX). Margin-intensive; intended for live mode only with
strict SL. Disabled in paper mode by the engine.
"""
from __future__ import annotations

import logging
import time

from ..market.inst_helpers import inst_cfg
from .base import Signal, Strategy, StrategyContext

log = logging.getLogger(__name__)


class ContrarianStrategy(Strategy):
    name = "contrarian"

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        cfg = self.config
        iv_min = float(cfg.get("iv_min_percentile", 82))
        if ctx.iv_percentile < iv_min:
            return []
        adx = ctx.indicators.get("adx_5m")
        if adx is None or adx > float(cfg.get("adx_max", 22)):
            return []
        if ctx.chain is None or ctx.spot <= 0:
            return []
        interval = inst_cfg(ctx.underlying, ctx.config, "strike_interval", 50)
        atm = ctx.chain.atm_strike(interval)
        width_sigma = float(cfg.get("width_sigma", 1.5))
        dte_days = 7.0
        em = ctx.chain.expected_move_1sigma(interval, dte_days) * width_sigma
        dist = round(em / interval) * interval
        if dist < interval:
            dist = interval
        expiry = ctx.chain.expiry
        signals = []
        for ot, strike in (("CE", atm + dist), ("PE", atm - dist)):
            row = ctx.chain.get(strike, ot)
            signals.append(Signal(
                strategy=self.name, side="SELL", option_type=ot,
                underlying=ctx.underlying, expiry=expiry, strike=strike,
                reason="IV pct=%.0f rich + ADX=%.0f range, sell %s@%.0f" % (ctx.iv_percentile, adx, ot, strike),
                ts=time.time(), entry_price_hint=row.ltp if row else 0.0,
                meta={"atm": atm, "dist": dist,
                      "sl_pct": float(cfg.get("sl_premium_pct", 25)),
                      "target_pct": float(cfg.get("target_premium_pct", 35)),
                      "requires_live": True},
            ))
        return signals
