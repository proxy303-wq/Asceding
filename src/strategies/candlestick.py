"""Candlestick-reversal strategy: buy options on classic reversal patterns that
appear in a trend-pullback context with cheap IV and a confirming oscillator."""
from __future__ import annotations

import logging
import time

from ..analytics.patterns import (BEARISH_PATTERNS, BULLISH_PATTERNS,
                                  analyze_candles)
from ..market.inst_helpers import inst_cfg
from .base import Signal, Strategy, StrategyContext

log = logging.getLogger(__name__)


class CandlestickStrategy(Strategy):
    name = "candlestick"

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        cfg = self.config
        ind = ctx.indicators
        if ctx.chain is None or ctx.spot <= 0:
            return []

        # entry time window (slightly wider than momentum: reversals also work midday)
        lt = time.localtime(ctx.ts)
        hm = lt.tm_hour * 100 + lt.tm_min
        if hm < int(cfg.get("entry_hm_start", 925)) or hm > int(cfg.get("entry_hm_end", 1400)):
            return []

        series = (ctx.series_5m if cfg.get("use_5m", True) and ctx.series_5m
                  else ctx.series_1m)
        if series is None or len(series.candles) < 6:
            return []
        candles = list(series.candles)
        opens = [c.open for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]

        pats = analyze_candles(opens, highs, lows, closes)
        if not pats:
            return []
        names = {p["pattern"] for p in pats}
        # neutral patterns (doji/spinning top) are ambiguous on their own; only
        # directional patterns decide. If only neutrals appear, context+RSI decides.
        neutral = {"doji", "spinning_top"}
        directional = names - neutral
        bull_d = bool(directional & BULLISH_PATTERNS)
        bear_d = bool(directional & BEARISH_PATTERNS)
        bull = bull_d and not bear_d
        bear = bear_d and not bull_d
        if not bull and not bear and (names & neutral):
            bull = True
            bear = True

        # trend context: reversal must come against the recent move
        lookback = int(cfg.get("trend_bars", 8))
        min_against = max(3, int(lookback * float(cfg.get("trend_confirmation", 0.6))))
        down_count = sum(1 for i in range(-lookback, 0) if closes[i] < closes[i - 1])
        up_count = lookback - down_count
        r = ind.get("rsi_1m")
        iv_ok = ctx.iv_percentile <= float(cfg.get("iv_max_percentile", 70))

        interval = inst_cfg(ctx.underlying, ctx.config, "strike_interval", 50)
        atm = ctx.chain.atm_strike(interval)
        offset = int(cfg.get("strike_offset", 1))
        expiry = ctx.chain.expiry

        signals = []
        if bull and down_count >= min_against and r is not None and r <= float(cfg.get("rsi_max_bull", 45)) and iv_ok:
            strike = atm + offset * interval
            row = ctx.chain.get(strike, "CE")
            signals.append(Signal(
                strategy=self.name, side="BUY", option_type="CE",
                underlying=ctx.underlying, expiry=expiry, strike=strike,
                reason="candle %s after downtrend (RSI %.0f, IV pct %.0f)" % (
                    "+".join(sorted(names)), r or 0, ctx.iv_percentile),
                ts=time.time(), entry_price_hint=row.ltp if row else 0.0,
                meta={"patterns": sorted(names), "atm": atm, "interval": interval},
            ))
        elif bear and up_count >= min_against and r is not None and r >= float(cfg.get("rsi_min_bear", 55)) and iv_ok:
            strike = atm - offset * interval
            row = ctx.chain.get(strike, "PE")
            signals.append(Signal(
                strategy=self.name, side="BUY", option_type="PE",
                underlying=ctx.underlying, expiry=expiry, strike=strike,
                reason="candle %s after uptrend (RSI %.0f, IV pct %.0f)" % (
                    "+".join(sorted(names)), r or 0, ctx.iv_percentile),
                ts=time.time(), entry_price_hint=row.ltp if row else 0.0,
                meta={"patterns": sorted(names), "atm": atm, "interval": interval},
            ))
        return signals
