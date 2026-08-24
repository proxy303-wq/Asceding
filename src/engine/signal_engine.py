"""Signal engine: run strategies against live market state, apply risk filters,
produce trade intents for execution."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime as _dt
from typing import Optional

from ..broker.base import ChainSnapshot
from ..market.candles import CandleSeries
from ..strategies.base import Signal, Strategy, StrategyContext
from .risk import RiskManager

log = logging.getLogger(__name__)


@dataclass
class MarketState:
    underlying: str
    spot: float
    ts: float
    chain: Optional[ChainSnapshot] = None
    series_1m: Optional[CandleSeries] = None
    series_5m: Optional[CandleSeries] = None
    daily: Optional[CandleSeries] = None
    iv_percentile: float = 50.0
    indicators: dict = field(default_factory=dict)
    underlying_cfg: dict = field(default_factory=dict)
    regime: str = "RANGE"


@dataclass
class TradeIntent:
    signal: Signal
    security_id: str
    premium_entry: float
    qty: int
    sl_price: float
    target_price: float
    lot_size: int
    product_type: str = "INTRADAY"
    requires_live: bool = False


def _realized_vol_annual(series) -> float:
    """Annualized realized volatility from the last 30 1m closes."""
    import math
    try:
        closes = series.closes()[-31:]
        if len(closes) < 20:
            return 0.0
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if not rets:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return math.sqrt(var) * math.sqrt(375 * 252)   # minutes/day x days/year
    except Exception:
        return 0.0


class SignalEngine:
    def __init__(self, strategies: list[Strategy], risk: RiskManager,
                 instruments_cfg: dict, config: dict):
        self.strategies = strategies
        self.risk = risk
        self.instruments_cfg = instruments_cfg
        self.config = config
        self._last_signal: dict[str, float] = {}
        self._pending: dict[str, tuple] = {}
        self.cooldown_s = float(config.get("signal_cooldown_s", 900))
        sq = config.get("signal_quality", {})
        self.confirm_bars = int(sq.get("confirm_bars", 1))
        self.iv_rv_max = float(sq.get("iv_rv_max", 0.0) or 0.0)
        ml_cfg = config.get("ml_gate", {})
        self.ml_threshold = float(ml_cfg.get("threshold", 0.55))
        self.ml = None
        if ml_cfg.get("enabled", True):
            try:
                from ..ml.gate import MLGate
                self.ml = MLGate()
            except Exception as e:
                log.warning("ML gate init failed: %s", e)

    def _underlying_cfg(self, name: str) -> dict:
        for u in self.instruments_cfg:
            if u.get("underlying") == name:
                return u
        return {}

    def run(self, states: list[MarketState], open_positions: int, open_exposure: float,
            current_equity: float, now_hm: int | None = None) -> list[TradeIntent]:
        intents = []
        if now_hm is None:
            lt = time.localtime()
            now_hm = lt.tm_hour * 100 + lt.tm_min
        if self.confirm_bars > 1 and states:
            ts_now = states[0].ts
            self._pending = {k: v for k, v in self._pending.items() if ts_now - v[0] <= 300}
        for st in states:
            ucfg = st.underlying_cfg or self._underlying_cfg(st.underlying)
            st.regime = self._detect_regime(st)
            for strat in self.strategies:
                if not strat.enabled():
                    continue
                from ..analytics.regime import REGIME_STRATEGY_MAP
                allowed = REGIME_STRATEGY_MAP.get(strat.name, set())
                if allowed and st.regime not in allowed:
                    continue
                ctx = StrategyContext(
                    underlying=st.underlying, spot=st.spot, ts=st.ts,
                    chain=st.chain, iv_percentile=st.iv_percentile,
                    series_1m=st.series_1m, series_5m=st.series_5m,
                    daily_series=st.daily, indicators=st.indicators,
                    config=ucfg,
                )
                try:
                    signals = strat.evaluate(ctx)
                except Exception as e:
                    log.exception("strategy %s failed for %s", strat.name, st.underlying)
                    continue
                for sig in signals:
                    key = f"{strat.name}:{st.underlying}"
                    # 2-bar confirmation: the same signal must repeat within a few bars
                    # (keyed on side+type, not strike: theta-optimized selection may shift strikes)
                    if self.confirm_bars > 1:
                        pk = f"{key}:{sig.option_type}:{sig.side}"
                        pending = self._pending.get(pk)
                        if pending is None:
                            self._pending[pk] = (st.ts, sig)
                            continue
                        if st.ts - pending[0] > 300:
                            self._pending[pk] = (st.ts, sig)
                            continue
                        del self._pending[pk]
                    # cooldown per strategy+underlying, keyed on the state clock (st.ts is
                    # wall time in live mode, simulated bar time in backtests)
                    if st.ts - self._last_signal.get(key, 0) < self.cooldown_s:
                        continue
                    intent = self._to_intent(sig, st, ucfg, open_positions, open_exposure,
                                             current_equity, now_hm)
                    if intent is None:
                        continue
                    self._last_signal[key] = st.ts
                    intents.append(intent)
        return intents

    def _detect_regime(self, st: MarketState) -> str:
        try:
            from ..analytics.regime import detect_regime
            ind = st.indicators
            spot = st.spot or 0.0
            atr_pct = (ind.get("atr_1m") or 0.0) / spot * 100.0 if spot else 0.0
            atr_pct_avg = (ind.get("atr_ma_1m") or 0.0) / spot * 100.0 if spot else 0.0
            s = st.series_1m
            return detect_regime(s.closes() if s else [], s.highs() if s else [],
                                 s.lows() if s else [], ind.get("adx_5m"),
                                 ind.get("ema_slow_5m"), ind.get("ema_slow_prev_5m"),
                                 atr_pct, atr_pct_avg)
        except Exception:
            return "RANGE"

    def _conviction_score(self, sig: Signal, st: MarketState) -> float:
        """0-100: weighted count of independent confirmations (mirrors PrOxy's
        confidence gate but derived from this engine's own indicators)."""
        ind = st.indicators
        score = 0.0
        ef, es = ind.get("ema_fast_1m"), ind.get("ema_slow_1m")
        ef5, es5 = ind.get("ema_fast_5m"), ind.get("ema_slow_5m")
        r = ind.get("rsi_1m")
        atr = ind.get("atr_1m")
        atr_ma = ind.get("atr_ma_1m")
        bull = sig.option_type == "CE"
        # 5m trend agreement (25)
        if ef5 is not None and es5 is not None:
            if (ef5 > es5) == bull:
                score += 25
        # 1m trend agreement (20)
        if ef is not None and es is not None:
            if (ef > es) == bull:
                score += 20
        # RSI in the direction band (15)
        if r is not None:
            if bull and 50 <= r <= 72:
                score += 15
            elif not bull and 28 <= r <= 50:
                score += 15
        # cheap IV (15)
        if st.iv_percentile <= float(sig.meta.get("iv_max", 70)):
            score += 15
        # ATR expansion (15)
        if atr is not None and atr_ma and atr >= 1.05 * atr_ma:
            score += 15
        # volume/pattern boost (10): candlestick patterns or breakout carry weight
        if sig.meta.get("patterns") or sig.strategy in ("breakout", "candlestick"):
            score += 10
        return round(min(score, 100.0), 1)

    def _maybe_btst(self, sig: Signal, st: MarketState, d: float):
        """Mark a late-day signal as BTST (hold overnight) when it is 'solid':
        within the BTST window, expiry far enough, and 1m+5m trend agreement
        in the signal direction with RSI not at an extreme."""
        btst_cfg = self.config.get("btst", {})
        if not btst_cfg.get("enabled", True):
            return
        lt = _dt.fromtimestamp(st.ts)
        hm = lt.hour * 100 + lt.minute
        if not (int(btst_cfg.get("entry_hm_start", 1400)) <= hm <= int(btst_cfg.get("entry_hm_end", 1500))):
            return
        if d < float(btst_cfg.get("min_dte", 1.5)):
            return
        if sig.side != "BUY":
            return
        strong = True
        if btst_cfg.get("require_strong", True):
            ind = st.indicators
            ef, es = ind.get("ema_fast_1m"), ind.get("ema_slow_1m")
            ef5, es5 = ind.get("ema_fast_5m"), ind.get("ema_slow_5m")
            r = ind.get("rsi_1m")
            if sig.option_type == "CE":
                strong = ef is not None and es is not None and ef > es and \
                         ef5 is not None and es5 is not None and ef5 > es5 and \
                         r is not None and r < 75
            else:
                strong = ef is not None and es is not None and ef < es and \
                         ef5 is not None and es5 is not None and ef5 < es5 and \
                         r is not None and r > 25
        if strong:
            sig.meta["btst"] = True
            log.info("signal %s marked BTST (hold overnight)", sig.strategy)

    def _to_intent(self, sig: Signal, st: MarketState, ucfg: dict,
                   open_positions: int, open_exposure: float, current_equity: float,
                   now_hm: int) -> Optional[TradeIntent]:
        # --- quant guards: expiry-day theta trap + too-far expiries ---------
        from ..market.instruments import dte
        # use the state clock: bar time in backtests, wall time in live mode
        d = dte(sig.expiry, now=_dt.fromtimestamp(st.ts))
        risk = self.risk
        self._maybe_btst(sig, st, d)
        # ML win-probability gate
        try:
            from ..ml.features import extract_features
            feat = extract_features(st, sig, d)
            sig.meta["ml_features"] = feat
            if self.ml is not None and self.ml.ready:
                ok, prob = self.ml.should_take(feat, self.ml_threshold)
                if not ok:
                    log.info("signal %s blocked by ML gate (p=%.2f < %.2f)",
                             sig.strategy, prob or 0.0, self.ml_threshold)
                    return None
                sig.meta["ml_prob"] = prob
        except Exception as e:
            log.warning("ML gate evaluation failed: %s", e)
        if d < float(self.config.get("risk", {}).get("min_dte", 0.5)):
            log.info("signal %s skipped: only %.2f days to expiry", sig.strategy, d)
            return None
        if d > float(self.config.get("risk", {}).get("max_dte", 7)):
            log.info("signal %s skipped: expiry too far (%.1f days)", sig.strategy, d)
            return None
        if st.chain is None:
            return None
        interval = float(ucfg.get("strike_interval", 50))
        # conviction score (confidence gate): how many independent confirmations align
        sig.meta["conviction"] = self._conviction_score(sig, st)
        min_conv = float(self.config.get("signal_quality", {}).get("min_conviction", 0))
        if min_conv > 0 and sig.meta["conviction"] < min_conv:
            log.info("signal %s blocked: conviction %.0f < %.0f", sig.strategy,
                     sig.meta["conviction"], min_conv)
            return None
        # theta-aware strike selection: prefer ATM/ITM strikes with least time decay
        from ..market.selection import select_best_strike
        sel_cfg = self.config.get("strike_select", {})
        row = select_best_strike(st.chain, sig.option_type, interval, sel_cfg,
                                 hint_strike=sig.strike)
        if row is None:
            log.info("signal %s: no usable row for %s (strike select)", sig.strategy, sig.option_type)
            return None
        if row.strike != sig.strike:
            log.info("signal %s: strike %s %.0f -> %.0f (theta-optimized)",
                     sig.strategy, sig.option_type, sig.strike, row.strike)
            sig.strike = row.strike
        security_id = row.security_id
        if not security_id:
            log.warning("signal %s: no security_id for %s %.0f %s", sig.strategy, st.underlying, sig.strike, sig.option_type)
            return None
        security_id = row.security_id
        if not security_id:
            log.warning("signal %s: no security_id for %s %.0f %s", sig.strategy, st.underlying, sig.strike, sig.option_type)
            return None
        premium = row.ltp if row.ltp > 0 else sig.entry_price_hint
        if premium <= 0:
            log.info("signal %s: no premium for entry", sig.strategy)
            return None

        # IV vs realized-vol sanity (skip when premium is far above fair value)
        if self.iv_rv_max > 0 and st.chain is not None:
            iv_atm = st.chain.iv_atm(float(ucfg.get("strike_interval", 50)))
            rv = _realized_vol_annual(st.series_1m)
            if iv_atm > 0 and rv > 0 and iv_atm / rv > self.iv_rv_max:
                log.info("signal %s skipped: IV/RV %.2f > %.2f (premium too rich)",
                         sig.strategy, iv_atm / rv, self.iv_rv_max)
                return None

        # greeks at entry (delta/gamma/theta/vega/IV) ride along for monitoring
        sig.meta["greeks"] = {
            "delta": row.delta or 0.0, "gamma": row.gamma or 0.0,
            "theta": row.theta or 0.0, "vega": row.vega or 0.0,
            "iv": row.iv or 0.0, "expected_move": st.chain.expected_move_1sigma(
                float(ucfg.get("strike_interval", 50)), d) if st.chain else 0.0,
        }

        lot_size = int(ucfg.get("lot_size", 0) or 0)
        sl_pct = float(sig.meta.get("sl_pct", self.risk.sl_pct))
        scalp = self.config.get("risk", {}).get("scalp", {}) or {}
        target_pts = float(scalp.get("target_points", 0) or 0)
        sl_pts = float(scalp.get("sl_points", 0) or 0)
        t1_pts = float(scalp.get("t1_points", 0) or 0)
        t2_pts = float(scalp.get("t2_points", 0) or 0)
        if sig.side == "BUY" and (target_pts > 0 or t1_pts > 0):
            # point-based: SL at entry - sl_points; target T1(+pts)/T2(+pts) or simple target_points
            sl_pts_eff = sl_pts if sl_pts > 0 else float(scalp.get("sl_points", 0) or 0)
            sl_price = (premium - sl_pts_eff) if sl_pts_eff > 0 else premium * (1 - sl_pct)
            target_price = premium + (t2_pts if t2_pts > 0 else target_pts)
            fixed_lots = int(scalp.get("lots", 0) or 0)
            if fixed_lots > 0:
                qty = fixed_lots * lot_size if lot_size else fixed_lots
            elif sl_pts_eff > 0:
                qty = max(lot_size or 1, (int(self.risk.risk_per_trade / sl_pts_eff) // (lot_size or 1)) * (lot_size or 1))
            else:
                qty = self.risk.size_position(premium, st.underlying, lot_size, sl_pct)
            product = "INTRADAY"
            sig.meta["scalp_t1"] = t1_pts
            sig.meta["scalp_t2"] = t2_pts
        elif sig.side == "BUY":
            fixed_lots = int(scalp.get("lots", 0) or 0)
            if fixed_lots > 0:
                qty = fixed_lots * lot_size if lot_size else fixed_lots
            else:
                qty = self.risk.size_position(premium, st.underlying, lot_size, sl_pct)
            sl_price, target_price = self.risk.sl_target_prices(premium, sl_pct)
            tg_pct = float(self.config.get("risk", {}).get("target_gain_pct", 0) or 0)
            if tg_pct > 0:
                target_price = premium * (1 + tg_pct / 100.0)
            # optional delta-based SL: premium SL implied by the spot stop, via delta
            use_delta_sl = bool(self.config.get("strategies", {}).get(sig.strategy, {}).get("use_delta_sl", False))
            if use_delta_sl and row.delta:
                spot_atr = st.indicators.get("atr_1m")
                sl_atr_mult = float(sig.meta.get("sl_atr_mult", 0.35))
                if spot_atr:
                    spot_risk = sl_atr_mult * spot_atr
                    premium_sl = premium - spot_risk * min(abs(row.delta), 0.6)
                    if premium_sl > sl_price:      # tighter of the two stops
                        sl_price = premium_sl
                        sl_pct = (premium - sl_price) / premium
                        _, target_price = self.risk.sl_target_prices(premium, sl_pct)
            product = "INTRADAY"
        else:  # SELL (contrarian shorts) — margin product, live only
            qty = int(ucfg.get("lot_size", 0) or 0)
            sl_price = premium * (1 + float(sig.meta.get("sl_pct", 25)) / 100.0)
            target_price = premium * (1 - float(sig.meta.get("target_pct", 35)) / 100.0)
            product = "MARGIN"
        if qty <= 0:
            log.info("signal %s: zero qty", sig.strategy)
            return None

        premium_value = premium * qty
        ok, reason = self.risk.check_entry(open_positions, open_exposure, current_equity,
                                           premium_value, now_hm)
        if not ok:
            log.info("signal %s rejected by risk: %s", sig.strategy, reason)
            return None
        requires_live = bool(sig.meta.get("requires_live", False))
        return TradeIntent(
            signal=sig, security_id=security_id, premium_entry=premium, qty=qty,
            sl_price=sl_price, target_price=target_price, lot_size=lot_size,
            product_type=product, requires_live=requires_live,
        )
