"""Opening-range / prior-day breakout with volume + OI confirmation."""
from __future__ import annotations

import logging
import time

from ..market.inst_helpers import inst_cfg
from .base import Signal, Strategy, StrategyContext

log = logging.getLogger(__name__)


class BreakoutStrategy(Strategy):
    name = "breakout"

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        cfg = self.config
        ind = ctx.indicators
        or_high, or_low = ind.get("or_high"), ind.get("or_low")
        if not or_high or not or_low:
            return []
        if ctx.chain is None or ctx.spot <= 0:
            return []
        now = time.localtime()
        hm = now.tm_hour * 100 + now.tm_min
        valid_until = int(cfg.get("valid_until_hm", 1130))
        if hm > valid_until:
            return []

        atr5 = ind.get("atr_5m")
        if atr5 is None:
            return []
        interval = inst_cfg(ctx.underlying, ctx.config, "strike_interval", 50)
        atm = ctx.chain.atm_strike(interval)
        offset = int(cfg.get("strike_offset", 1))
        iv_max = float(cfg.get("iv_max_percentile", 70))
        oi_confirm = bool(cfg.get("oi_confirm", True))
        vol_surge = float(cfg.get("vol_surge_mult", 1.5))
        last = ctx.series_1m.last() if ctx.series_1m else None
        if last is None:
            return []
        vol_avg = ind.get("vol_avg_1m", 1.0) or 1.0

        up_break = ctx.spot > or_high + float(cfg.get("breakout_atr_mult", 0.3)) * atr5 and last.close > or_high
        dn_break = ctx.spot < or_low - float(cfg.get("breakout_atr_mult", 0.3)) * atr5 and last.close < or_low
        vol_ok = last.volume >= vol_surge * vol_avg
        iv_ok = ctx.iv_percentile <= iv_max
        oi_up = ctx.chain.oi_change_at(atm, "CE") > 0
        oi_dn = ctx.chain.oi_change_at(atm, "PE") > 0

        signals = []
        expiry = ctx.chain.expiry
        if up_break and vol_ok and iv_ok and (not oi_confirm or oi_up):
            strike = atm + offset * interval
            row = ctx.chain.get(strike, "CE")
            signals.append(Signal(
                strategy=self.name, side="BUY", option_type="CE",
                underlying=ctx.underlying, expiry=expiry, strike=strike,
                reason="OR breakout up (%.0f > %.0f) vol surge, OI+%s" % (ctx.spot, or_high, "y" if oi_up else "n"),
                ts=time.time(), entry_price_hint=row.ltp if row else 0.0,
                meta={"atm": atm, "or_high": or_high, "or_low": or_low, "max_hold_min": int(cfg.get("max_hold_min", 150))},
            ))
        elif dn_break and vol_ok and iv_ok and (not oi_confirm or oi_dn):
            strike = atm - offset * interval
            row = ctx.chain.get(strike, "PE")
            signals.append(Signal(
                strategy=self.name, side="SELL_OPTION" if False else "BUY", option_type="PE",
                underlying=ctx.underlying, expiry=expiry, strike=strike,
                reason="OR breakout down (%.0f < %.0f) vol surge, OI+%s" % (ctx.spot, or_low, "y" if oi_dn else "n"),
                ts=time.time(), entry_price_hint=row.ltp if row else 0.0,
                meta={"atm": atm, "or_high": or_high, "or_low": or_low, "max_hold_min": int(cfg.get("max_hold_min", 150))},
            ))
        return signals
