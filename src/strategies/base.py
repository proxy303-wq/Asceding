"""Strategy framework: Signal, StrategyContext, Strategy base class."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional

from ..broker.base import ChainSnapshot
from ..market.candles import CandleSeries


@dataclass
class Signal:
    strategy: str
    side: str                 # BUY | SELL
    option_type: str          # CE | PE
    underlying: str
    expiry: str
    strike: float
    reason: str
    ts: float
    entry_price_hint: float = 0.0    # option premium at signal time (for sizing)
    meta: dict = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.strategy}:{self.underlying}:{self.option_type}:{self.strike}"


@dataclass
class StrategyContext:
    underlying: str
    spot: float
    ts: float
    chain: Optional[ChainSnapshot] = None
    iv_percentile: float = 50.0
    series_1m: Optional[CandleSeries] = None
    series_5m: Optional[CandleSeries] = None
    daily_series: Optional[CandleSeries] = None
    indicators: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)


class Strategy(abc.ABC):
    name: str = "base"

    def __init__(self, cfg: dict, config: dict):
        self.cfg = cfg          # global config
        self.config = config    # this strategy's section

    @abc.abstractmethod
    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        ...

    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))
