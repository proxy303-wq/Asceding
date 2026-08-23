"""Execution manager: enter/exits with SL+target, monitor open trades, P&L capture."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..broker.base import Broker, Quote
from ..config import ist_now
from .risk import RiskManager
from .signal_engine import TradeIntent

log = logging.getLogger(__name__)


@dataclass
class ActiveTrade:
    security_id: str
    symbol: str
    underlying: str
    option_type: str
    strike: float
    expiry: str
    strategy: str
    side: str
    qty: int
    entry_price: float
    entry_time: float
    sl_price: float = 0.0
    target_price: float = 0.0
    super_order_id: str = ""
    status: str = "OPEN"
    exit_reason: str = ""
    pnl: float = 0.0
    baseline_realized: float = 0.0
    btst: bool = False
    segment: str = "FNO"          # FNO (index options) | EQ (stock BTST)
    meta: dict = field(default_factory=dict)


def trailing_sl(entry_price: float, current_sl: float, peak: float, profit: float,
               r_val: float, arm_r: float, step_r: float, breakeven_r: float) -> float:
    """Ratchet the stop-loss for a long option: arm after 1R, keep SL within
    step_r x R of the peak, never below breakeven once triggered, never lower it."""
    new_sl = current_sl
    if r_val > 0:
        if profit >= arm_r * r_val:
            new_sl = max(new_sl, peak - step_r * r_val)
        if profit >= breakeven_r * r_val:
            new_sl = max(new_sl, entry_price)
    return round(new_sl, 2)


def lock_trail_sl(entry_price: float, current_sl: float, peak: float, profit_pct: float,
               arm_pct: float, trail_pct: float, breakeven_pct: float) -> float:
    """Premium-% lock-profit trail (PrOxy sweep insight): once profit reaches
    arm_pct% of entry premium, keep SL within trail_pct% of the peak; after
    breakeven_pct% profit, never lose money."""
    new_sl = current_sl
    if profit_pct >= arm_pct:
        new_sl = max(new_sl, peak - entry_price * trail_pct / 100.0)
    if profit_pct >= breakeven_pct:
        new_sl = max(new_sl, entry_price)
    return round(new_sl, 2)


class ExecutionManager:
    def __init__(self, broker: Broker, risk: RiskManager, store, mode: str):
        self.broker = broker
        self.risk = risk
        self.store = store
        self.mode = mode
        self.open: dict[str, ActiveTrade] = {}
        self.quote_provider: Optional[Callable[[str], Quote]] = None
        self.on_trade_closed: Optional[Callable[[dict], None]] = None

    def track_stock(self, security_id: str, symbol: str, qty: int, entry_price: float,
                    sl_price: float, target_price: float) -> tuple[bool, str]:
        """Open a stock (EQ) BTST position with SL/target monitoring (no super order)."""
        if security_id in self.open:
            return False, "already tracking"
        if self.mode == "paper":
            res = self.broker.place_order(security_id=security_id, transaction_type="BUY",
                                          quantity=qty, order_type="MARKET",
                                          product_type="CNC", exchange_segment="NSE_EQ",
                                          tag=symbol)
            if not res.ok:
                return False, res.message
            pos = self.broker.positions.get(security_id)
            if pos:
                pos.sl_price = float(sl_price)
                pos.target_price = float(target_price)
            fill = pos.entry_price if pos else entry_price
        else:
            res = self.broker.place_order(security_id=security_id, transaction_type="BUY",
                                          quantity=qty, order_type="LIMIT",
                                          price=round(entry_price, 2),
                                          product_type="CNC", exchange_segment="NSE_EQ",
                                          tag=symbol)
            if not res.ok:
                return False, res.message
            fill = entry_price
        trade = ActiveTrade(
            security_id=security_id, symbol=symbol, underlying=symbol, option_type="",
            strike=0.0, expiry="", strategy="stock_btst", side="BUY", qty=qty,
            entry_price=fill, entry_time=time.time(), sl_price=float(sl_price),
            target_price=float(target_price), segment="EQ",
        )
        trade.meta["peak"] = fill
        trade.meta["max_hold_min"] = 0
        self.open[security_id] = trade
        log.info("STOCK BTST ENTER %s qty=%d @ %.2f sl=%.2f tp=%.2f",
                 symbol, qty, fill, sl_price, target_price)
        return True, "tracked"

    def set_quote_provider(self, fn: Callable[[str], Quote]):
        self.quote_provider = fn
        if hasattr(self.broker, "set_quote_provider"):
            self.broker.set_quote_provider(fn)

    # ---------- entry ----------
    def enter(self, intent: TradeIntent) -> tuple[bool, str]:
        sid = intent.security_id
        if sid in self.open:
            return False, "already tracking %s" % sid
        if intent.requires_live and self.mode != "live":
            return False, "signal requires live mode (margin/short), skipping in paper"
        sig = intent.signal
        # BTST: hold overnight on solid late-day signals -> positional product
        is_btst = bool(intent.signal.meta.get("btst", False))
        if is_btst and intent.signal.side == "BUY":
            intent.product_type = "MARGIN"
        # exit-mode aware target: trail/reversal modes use a far backstop target
        if self.risk.exit_mode in ("trail", "reversal", "trail_and_reversal"):
            intent.target_price = self.risk.exit_target_price(intent.premium_entry, intent.sl_price)
        if intent.signal.side == "BUY":
            res = self.broker.place_super_order(
                security_id=sid, transaction_type="BUY", quantity=intent.qty,
                order_type="LIMIT", price=round(intent.premium_entry, 2),
                target_price=round(intent.target_price, 2),
                stop_loss_price=round(intent.sl_price, 2),
                product_type=intent.product_type, exchange_segment="NSE_FNO",
                tag=f"{sig.underlying}-{sig.option_type}{int(sig.strike)}",
            )
        else:
            res = self.broker.place_super_order(
                security_id=sid, transaction_type="SELL", quantity=intent.qty,
                order_type="LIMIT", price=round(intent.premium_entry, 2),
                target_price=round(intent.target_price, 2),
                stop_loss_price=round(intent.sl_price, 2),
                product_type=intent.product_type, exchange_segment="NSE_FNO",
                tag=f"{sig.underlying}-{sig.option_type}{int(sig.strike)}",
            )
        if not res.ok:
            log.error("order failed for %s: %s", sid, res.message)
            return False, res.message
        baseline = 0.0
        try:
            for p in self.broker.get_positions():
                if p.security_id == sid:
                    baseline = p.realized
        except Exception:
            pass
        trade = ActiveTrade(
            security_id=sid, symbol=f"{sig.underlying} {sig.option_type} {int(sig.strike)}",
            underlying=sig.underlying, option_type=sig.option_type, strike=sig.strike,
            expiry=sig.expiry, strategy=sig.strategy, side=sig.side, qty=intent.qty,
            entry_price=intent.premium_entry, entry_time=time.time(),
            sl_price=intent.sl_price, target_price=intent.target_price,
            super_order_id=res.order_id, baseline_realized=baseline,
        )
        trade.meta["peak"] = intent.premium_entry
        trade.meta["r"] = self.risk.r_value(intent.premium_entry, intent.sl_price)
        trade.meta["max_hold_min"] = int(sig.meta.get("max_hold_min", 0))
        trade.btst = is_btst
        if is_btst:
            log.info("BTST hold scheduled for %s (overnight, product MARGIN)", sid)
        self.open[sid] = trade
        log.info("ENTER %s %s %d lots qty=%d @ %.2f sl=%.2f tp=%.2f (order %s)",
                 trade.symbol, sig.side, trade.qty, trade.qty, trade.entry_price,
                 trade.sl_price, trade.target_price, res.order_id)
        return True, res.order_id

    # ---------- monitoring ----------
    def monitor(self, broker_positions: Optional[list] = None, indicators: Optional[dict] = None):
        """Reconcile open trades: paper SL/TP engine + position-based close detection."""
        self._manage_exits(indicators)
        if self.mode == "paper" and hasattr(self.broker, "mark_paper_exits"):
            self.broker.mark_paper_exits()
            # detect closes from paper trade log
            try:
                for t in self.broker.trades:
                    sid = t["security_id"]
                    if sid in self.open and self.open[sid].status == "OPEN":
                        self._finalize(sid, t["exit_price"], t["pnl"], t["reason"])
            except Exception as e:
                log.warning("paper reconcile failed: %s", e)
        else:
            # live: detect closes via broker positions
            try:
                positions = broker_positions if broker_positions is not None else self.broker.get_positions()
                by_sid = {p.security_id: p for p in positions}
                for sid, tr in list(self.open.items()):
                    if tr.status != "OPEN":
                        continue
                    pos = by_sid.get(sid)
                    if pos is None or pos.net_qty == 0:
                        pnl = pos.realized - tr.baseline_realized if pos else 0.0
                        self._finalize(sid, 0.0, pnl, "POSITION_CLOSED")
                    else:
                        q = self.quote_provider(sid) if self.quote_provider else None
                        ltp = q.ltp if q else tr.entry_price
                        tr.meta["ltp"] = ltp
                        tr.meta["unrealized"] = (ltp - tr.entry_price) * tr.qty * (1 if tr.side == "BUY" else -1)
            except Exception as e:
                log.warning("live reconcile failed: %s", e)

    def _manage_exits(self, indicators: Optional[dict] = None):
        """Trailing stop, breakeven, reversal-confirmation exit, time stop."""
        if not self.open:
            return
        r_cfg = self.risk
        trail_on = r_cfg.trail_enabled
        mode = r_cfg.exit_mode
        for sid, tr in list(self.open.items()):
            if tr.status != "OPEN":
                continue
            q = self.quote_provider(sid) if self.quote_provider else None
            ltp = q.ltp if q else tr.meta.get("ltp", 0.0)
            if ltp <= 0:
                continue
            sign = 1 if tr.side == "BUY" else -1
            r_val = tr.meta.get("r", 0.0)
            profit = (ltp - tr.entry_price) * sign
            tr.meta["peak"] = max(tr.meta.get("peak", tr.entry_price), ltp) if sign > 0 else                               min(tr.meta.get("peak", tr.entry_price), ltp)
            peak = tr.meta["peak"]

            new_sl = tr.sl_price
            if trail_on and r_cfg.lock_enabled and tr.entry_price > 0:
                profit_pct = profit / tr.entry_price * 100.0
                new_sl = lock_trail_sl(tr.entry_price, tr.sl_price, peak, profit_pct,
                                       r_cfg.lock_arm_pct, r_cfg.lock_trail_pct,
                                       r_cfg.lock_breakeven_pct)
            elif trail_on:
                new_sl = trailing_sl(tr.entry_price, tr.sl_price, peak, profit, r_val,
                                     r_cfg.trail_arm_r, r_cfg.trail_step_r, r_cfg.breakeven_r)
            if new_sl != tr.sl_price:
                self._apply_sl(tr, new_sl)

            # time stop (strategy max_hold_min)
            # stock BTST legs: simple SL / target / time management (no trail/reversal)
            if tr.segment == "EQ":
                if tr.sl_price > 0 and ltp <= tr.sl_price:
                    self.exit_security(sid, "STOCK_SL_HIT")
                elif tr.target_price > 0 and ltp >= tr.target_price:
                    self.exit_security(sid, "STOCK_TARGET_HIT")
                continue

            max_hold = tr.meta.get("max_hold_min", 0)
            if max_hold > 0 and time.time() - tr.entry_time > max_hold * 60:
                log.info("time stop for %s after %d min", sid, max_hold)
                self.exit_security(sid, "TIME_STOP")
                continue

            # reversal confirmation exit: only for WINNERS, and only after the
            # trend-flip persists (2 min) so whipsaws don't knock us out early
            if mode in ("reversal", "trail_and_reversal") and indicators:
                ind = indicators.get(tr.underlying)
                if ind and profit > 0:
                    ef5, es5 = ind.get("ema_fast_5m"), ind.get("ema_slow_5m")
                    if ef5 is not None and es5 is not None:
                        reversal = (ef5 < es5) if sign > 0 else (ef5 > es5)
                        if reversal:
                            if tr.meta.get("flip_ts") is None:
                                tr.meta["flip_ts"] = time.time()
                            elif time.time() - tr.meta["flip_ts"] >= 120:
                                log.info("reversal confirmed for %s (5m EMA flip held 2m)", sid)
                                self.exit_security(sid, "REVERSAL_EXIT")
                                continue
                        else:
                            tr.meta["flip_ts"] = None
            if mode in ("target",) and r_cfg.target_mult_r <= 0 and tr.target_price > 0 and ltp >= tr.target_price:
                self.exit_security(sid, "TARGET_HIT")

    def _apply_sl(self, tr: "ActiveTrade", new_sl: float):
        """Move the stop: paper updates the position SL; live moves the SL leg."""
        tr.sl_price = new_sl
        if self.mode == "paper" and hasattr(self.broker, "positions"):
            pos = self.broker.positions.get(tr.security_id)
            if pos:
                pos.sl_price = new_sl
                log.info("trail %s: SL %.2f -> %.2f (peak %.2f)", tr.security_id, pos.sl_price, new_sl, tr.meta.get("peak", 0))
        elif self.mode == "live" and tr.super_order_id and hasattr(self.broker, "modify_super_order"):
            res = self.broker.modify_super_order(tr.super_order_id, "STOP_LOSS_LEG", new_sl)
            if not res.ok:
                log.warning("live trail SL update failed for %s: %s", tr.security_id, res.message)

    def check_time_exit(self):
        now = ist_now()
        hm = now.hour * 100 + now.minute
        if hm >= self.risk.time_exit and self.open:
            log.info("time exit triggered at %04d", hm)
            self.exit_non_btst("TIME_EXIT")

    def exit_non_btst(self, reason: str):
        for sid, tr in list(self.open.items()):
            if not tr.btst and tr.segment != "EQ":
                self.exit_security(sid, reason)

    def exit_btst(self, reason: str):
        for sid, tr in list(self.open.items()):
            if tr.btst:
                self.exit_security(sid, reason)

    def check_day_halt(self, current_equity: float):
        if self.risk.halted and self.open:
            log.warning("risk halted day -> exiting all")
            self.exit_all("RISK_HALT")

    # ---------- exits ----------
    def exit_security(self, security_id: str, reason: str):
        tr = self.open.get(security_id)
        if tr is None or tr.status != "OPEN":
            return
        q = self.quote_provider(security_id) if self.quote_provider else None
        price = q.mid if q and q.mid > 0 else tr.entry_price
        side = "SELL" if tr.side == "BUY" else "BUY"
        res = self.broker.place_order(security_id=security_id, transaction_type=side,
                                      quantity=tr.qty, order_type="MARKET",
                                      product_type="INTRADAY", exchange_segment="NSE_FNO",
                                      tag=reason)
        if res.ok:
            pnl = (price - tr.entry_price) * tr.qty * (1 if tr.side == "BUY" else -1)
            self._finalize(security_id, price, pnl, reason)
        else:
            log.error("exit failed for %s: %s", security_id, res.message)

    def exit_all(self, reason: str):
        for sid in list(self.open.keys()):
            self.exit_security(sid, reason)

    def _finalize(self, security_id: str, exit_price: float, pnl: float, reason: str):
        tr = self.open.get(security_id)
        if tr is None or tr.status != "OPEN":
            return
        tr.status = "CLOSED"
        tr.exit_reason = reason
        tr.pnl = pnl
        self.store.record_trade(
            security_id=tr.security_id, symbol=tr.symbol, underlying=tr.underlying,
            option_type=tr.option_type, strike=tr.strike, expiry=tr.expiry,
            strategy=tr.strategy, entry_price=tr.entry_price, exit_price=exit_price,
            quantity=tr.qty, pnl=pnl, exit_reason=reason, meta=tr.meta,
        )
        # label the ML sample captured at signal time - barrier-based (FinLab style):
        # label 1 if a profit barrier was hit first, 0 if the stop barrier was hit;
        # time/reversal exits keep the pnl sign as a soft label.
        feat = tr.meta.get("ml_features")
        if feat:
            try:
                profit_reasons = ("TARGET_HIT", "STOCK_TARGET_HIT", "LOCK_PROFIT", "REVERSAL_EXIT", "BTST_OPEN_EXIT")
                stop_reasons = ("SL_HIT", "STOCK_SL_HIT", "TIME_STOP")
                if reason in profit_reasons:
                    label = 1
                elif reason in stop_reasons:
                    label = 0
                else:
                    label = 1 if pnl > 0 else 0
                self.store.record_ml_sample(tr.strategy, tr.underlying, feat,
                                            label=label, outcome=pnl, exit_reason=reason)
            except Exception as e:
                log.warning("ml sample record failed: %s", e)
        log.info("EXIT %s %s pnl=%.2f reason=%s", tr.symbol, tr.side, pnl, reason)
        if self.on_trade_closed:
            try:
                self.on_trade_closed({
                    "symbol": tr.symbol, "strike": tr.strike, "option_type": tr.option_type,
                    "strategy": tr.strategy, "entry_price": tr.entry_price,
                    "exit_price": exit_price, "qty": tr.qty, "pnl": pnl,
                    "exit_reason": reason, "greeks": tr.meta.get("greeks", {}),
                })
            except Exception as e:
                log.warning("on_trade_closed callback failed: %s", e)
        del self.open[security_id]

    def open_trades_snapshot(self) -> list[dict]:
        out = []
        for t in self.open.values():
            if t.status != "OPEN":
                continue
            d = {
                "security_id": t.security_id, "symbol": t.symbol, "underlying": t.underlying,
                "option_type": t.option_type, "strike": t.strike, "expiry": t.expiry,
                "strategy": t.strategy, "side": t.side, "qty": t.qty,
                "entry_price": t.entry_price, "sl_price": t.sl_price, "target_price": t.target_price,
                "entry_time": t.entry_time, "status": t.status, "pnl": t.pnl,
                "unrealized": t.meta.get("unrealized", 0.0), "ltp": t.meta.get("ltp", 0.0),
                "greeks": t.meta.get("greeks", {}),
                "btst": t.btst,
                "segment": t.segment,
            }
            out.append(d)
        return out
