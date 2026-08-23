"""Paper broker: simulated fills on live quotes with slippage, SL/TP monitoring hooks.

No real orders are placed. Positions and cash live in memory (+ optional SQLite log
via the engine). Intended as the default, safe mode for validation.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .base import Broker, Funds, OrderResult, Position, Quote

log = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    security_id: str
    symbol: str
    qty: int
    entry_price: float
    entry_time: float
    sl_price: float = 0.0
    target_price: float = 0.0
    realized_pnl: float = 0.0
    super_order_id: str = ""
    exits: list = field(default_factory=list)


class PaperBroker(Broker):
    def __init__(self, initial_capital: float, quote_provider: Optional[Callable[[str], Quote]] = None,
                 slippage_bps: float = 2.0):
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.quote_provider = quote_provider
        self.slippage_bps = slippage_bps
        self.positions: dict[str, PaperPosition] = {}
        self.orders: list[dict] = []
        self.trades: list[dict] = []

    def set_quote_provider(self, fn: Callable[[str], Quote]):
        self.quote_provider = fn

    def _quote(self, security_id: str) -> Optional[Quote]:
        if self.quote_provider is None:
            return None
        try:
            return self.quote_provider(security_id)
        except Exception:
            return None

    def _fill_price(self, security_id: str, side: str) -> float:
        q = self._quote(security_id)
        if q is None or q.ltp <= 0:
            raise RuntimeError("no quote for %s" % security_id)
        slip = self.slippage_bps / 1e4
        if side == "BUY":
            base = q.ask if q.ask > 0 else q.ltp
            return base * (1 + slip)
        base = q.bid if q.bid > 0 else q.ltp
        return base * (1 - slip)

    def place_order(self, security_id, transaction_type, quantity, order_type="LIMIT", price=0.0,
                    trigger_price=0.0, product_type="INTRADAY", exchange_segment="NSE_FNO",
                    validity="DAY", tag=""):
        try:
            fill = self._fill_price(security_id, transaction_type)
            qty = int(quantity)
            value = fill * qty
            if transaction_type == "BUY":
                if value > self.cash:
                    return OrderResult.fail("insufficient paper cash: need %.0f have %.0f" % (value, self.cash))
                self.cash -= value
                pos = self.positions.get(security_id)
                if pos is None:
                    pos = PaperPosition(security_id=security_id, symbol=tag or security_id,
                                        qty=0, entry_price=0.0, entry_time=time.time())
                    self.positions[security_id] = pos
                total_cost = pos.entry_price * pos.qty + value
                pos.qty += qty
                pos.entry_price = total_cost / pos.qty if pos.qty else 0.0
            else:
                pos = self.positions.get(security_id)
                if pos is None or pos.qty < qty:
                    return OrderResult.fail("paper short/insufficient qty for %s" % security_id)
                pnl = (fill - pos.entry_price) * qty
                pos.qty -= qty
                pos.realized_pnl += pnl
                self.cash += value
                pos.exits.append({"qty": qty, "price": fill, "ts": time.time(), "pnl": pnl})
                if pos.qty == 0:
                    self._close_trade(pos, tag or "SELL_EXIT", fill)
            oid = "PAPER-" + uuid.uuid4().hex[:12]
            self.orders.append({"order_id": oid, "security_id": security_id, "side": transaction_type,
                                "qty": qty, "price": fill, "ts": time.time()})
            return OrderResult(order_id=oid, status="TRADED", raw={"fill_price": fill})
        except Exception as e:
            log.exception("paper order failed")
            return OrderResult.fail(str(e))

    def place_super_order(self, security_id, transaction_type, quantity, order_type="LIMIT", price=0.0,
                          target_price=0.0, stop_loss_price=0.0, product_type="INTRADAY",
                          exchange_segment="NSE_FNO", tag=""):
        res = self.place_order(security_id, transaction_type, quantity, order_type, price,
                               product_type=product_type, exchange_segment=exchange_segment, tag=tag)
        if res.ok and transaction_type == "BUY":
            pos = self.positions.get(security_id)
            if pos:
                pos.sl_price = float(stop_loss_price)
                pos.target_price = float(target_price)
                pos.super_order_id = res.order_id
        return res

    def cancel_order(self, order_id):
        return OrderResult(order_id=order_id, status="CANCELLED")

    def get_positions(self) -> list[Position]:
        out = []
        for sid, pos in self.positions.items():
            if pos.qty == 0:
                continue
            q = self._quote(sid)
            ltp = q.ltp if q else pos.entry_price
            out.append(Position(
                security_id=sid, symbol=pos.symbol, exchange_segment="NSE_FNO",
                net_qty=pos.qty, buy_avg=pos.entry_price, sell_avg=0.0,
                unrealized=(ltp - pos.entry_price) * pos.qty, realized=pos.realized_pnl,
                multiplier=1,
            ))
        return out

    def get_funds(self) -> Funds:
        unreal = sum(p.unrealized for p in self.get_positions())
        return Funds(available_balance=self.cash + unreal, utilized_amount=0.0,
                     withdrawable_balance=self.cash)

    def mark_paper_exits(self):
        """Close paper positions that hit SL/target (called by engine on each quote tick).
        place_order(SELL) already closes the position and records the trade."""
        for sid, pos in list(self.positions.items()):
            if pos.qty == 0:
                continue
            q = self._quote(sid)
            if q is None or q.ltp <= 0:
                continue
            if pos.sl_price > 0 and q.ltp <= pos.sl_price:
                res = self.place_order(sid, "SELL", pos.qty, order_type="MARKET", tag="SL_HIT")
                if res.ok:
                    log.info("paper SL hit %s at %.2f", sid, pos.sl_price)
            elif pos.target_price > 0 and q.ltp >= pos.target_price:
                res = self.place_order(sid, "SELL", pos.qty, order_type="MARKET", tag="TARGET_HIT")
                if res.ok:
                    log.info("paper target hit %s at %.2f", sid, pos.target_price)

    def _close_trade(self, pos: PaperPosition, reason: str, exit_price: float):
        self.trades.append({
            "security_id": pos.security_id, "symbol": pos.symbol,
            "qty": pos.qty, "entry_price": pos.entry_price, "exit_price": exit_price,
            "pnl": pos.realized_pnl, "reason": reason, "ts": time.time(),
        })
        del self.positions[pos.security_id]
