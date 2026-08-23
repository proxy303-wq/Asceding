"""Broker abstraction: data models + execution interface (live DHAN and paper)."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Quote:
    security_id: str
    symbol: str
    ltp: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: float = 0.0
    oi: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    ts: int = 0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.ltp


@dataclass
class OptionRow:
    security_id: str
    symbol: str
    underlying: str
    expiry: str
    strike: float
    option_type: str          # CE / PE
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    volume: float = 0.0
    oi: float = 0.0
    oi_change: float = 0.0
    iv: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.ltp


@dataclass
class ChainSnapshot:
    underlying: str
    expiry: str
    spot: float
    ts: int
    rows: dict = field(default_factory=dict)   # (strike, 'CE'|'PE') -> OptionRow
    raw: dict = field(default_factory=dict)

    def atm_strike(self, interval: float = 50.0) -> float:
        return round(self.spot / interval) * interval

    def get(self, strike: float, ot: str) -> Optional[OptionRow]:
        return self.rows.get((float(strike), ot.upper()))

    def iv_atm(self, interval: float = 50.0) -> float:
        k = self.atm_strike(interval)
        call = self.get(k, "CE")
        put = self.get(k, "PE")
        ivs = [r.iv for r in (call, put) if r and r.iv and r.iv > 0]
        return sum(ivs) / len(ivs) if ivs else 0.0

    def expected_move_1sigma(self, interval: float = 50.0, dte_days: float = 7.0) -> float:
        iv = self.iv_atm(interval)
        if iv <= 0:
            return 0.0
        import math
        return self.spot * iv * math.sqrt(max(dte_days, 0.0) / 365.0)

    def oi_change_at(self, strike: float, ot: str) -> float:
        r = self.get(strike, ot)
        return r.oi_change if r else 0.0


@dataclass
class OrderResult:
    order_id: str
    status: str
    raw: dict = field(default_factory=dict)
    ok: bool = True
    message: str = ""

    @classmethod
    def fail(cls, message: str, raw: dict = None) -> "OrderResult":
        return cls(order_id="", status="REJECTED", raw=raw or {}, ok=False, message=message)


@dataclass
class Position:
    security_id: str
    symbol: str
    exchange_segment: str
    net_qty: int
    buy_avg: float
    sell_avg: float
    unrealized: float
    realized: float
    multiplier: int = 1
    option_type: str = ""
    strike: float = 0.0
    expiry: str = ""

    @property
    def is_long(self) -> bool:
        return self.net_qty > 0


@dataclass
class Funds:
    available_balance: float
    utilized_amount: float = 0.0
    withdrawable_balance: float = 0.0
    raw: dict = field(default_factory=dict)


class Broker(abc.ABC):
    """Execution + account interface. Implementations: DhanLiveBroker, PaperBroker."""

    @abc.abstractmethod
    def place_order(self, security_id: str, transaction_type: str, quantity: int,
                    order_type: str = "LIMIT", price: float = 0.0, trigger_price: float = 0.0,
                    product_type: str = "INTRADAY", exchange_segment: str = "NSE_FNO",
                    validity: str = "DAY", tag: str = "") -> OrderResult: ...

    @abc.abstractmethod
    def place_super_order(self, security_id: str, transaction_type: str, quantity: int,
                          order_type: str = "LIMIT", price: float = 0.0,
                          target_price: float = 0.0, stop_loss_price: float = 0.0,
                          product_type: str = "INTRADAY", exchange_segment: str = "NSE_FNO",
                          tag: str = "") -> OrderResult: ...

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult: ...

    @abc.abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    def get_funds(self) -> Funds: ...
