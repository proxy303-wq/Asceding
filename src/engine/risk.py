"""Risk management for a 5L INR options account.

Rules (all configurable in config.yaml):
  - per-trade risk budget (INR)  -> position size = risk / (premium * sl_pct)
  - daily loss limit -> stop new entries for the day
  - optional daily profit target -> lock in gains
  - max concurrent positions + max exposure % of capital
  - max daily trades, no-new-entries-after, force time-exit
  - max drawdown from day-start equity
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

from ..config import ist_now

log = logging.getLogger(__name__)


def _hm(s: str) -> int:
    try:
        h, m = s.split(":")
        return int(h) * 100 + int(m)
    except Exception:
        return 1500


class RiskManager:
    def __init__(self, cfg: dict, store, lot_sizes: dict[str, int] | None = None,
                 clock=None):
        self.cfg = cfg
        self.store = store
        self.lot_sizes = lot_sizes or {}
        self._clock = clock or ist_now
        risk_cfg = cfg.get("risk", {})
        cap = cfg.get("capital", {})
        self.initial_capital = float(cap.get("initial", 500000))
        self.max_exposure_pct = float(cap.get("max_exposure_pct", 40))
        self.max_positions = int(cap.get("max_positions", 3))
        self.risk_per_trade = float(risk_cfg.get("risk_per_trade", 3000))
        self.daily_loss_limit = float(risk_cfg.get("daily_loss_limit", 5000))
        self.daily_profit_target = float(risk_cfg.get("daily_profit_target", 0))
        self.max_daily_trades = int(risk_cfg.get("max_daily_trades", 6))
        self.max_drawdown_pct = float(risk_cfg.get("max_drawdown_pct", 4.0))
        self.sl_pct = float(risk_cfg.get("default_sl_pct", 0.30))
        self.reward_risk = float(risk_cfg.get("reward_risk", 1.8))
        self.time_exit = _hm(risk_cfg.get("time_exit", "15:05"))
        self.no_new_after = _hm(risk_cfg.get("no_new_entries_after", "15:00"))
        self.max_consecutive_losses = int(risk_cfg.get("max_consecutive_losses", 2))
        self.exit_mode = risk_cfg.get("exit_mode", "trail_and_reversal")
        self.target_mult_r = float(risk_cfg.get("target_mult_r", 2.5))
        trail = risk_cfg.get("trailing", {})
        self.trail_enabled = bool(trail.get("enabled", True))
        self.trail_arm_r = float(trail.get("arm_after_r", 1.0))
        self.trail_step_r = float(trail.get("trail_step_r", 0.5))
        self.breakeven_r = float(trail.get("breakeven_after_r", 0.8))
        scalp = risk_cfg.get("scalp", {}) or {}
        self.scalp_t1 = float(scalp.get("t1_points", 0) or 0)
        self.scalp_t2 = float(scalp.get("t2_points", 0) or 0)
        self.scalp_sl = float(scalp.get("sl_points", 0) or 0)
        self.scalp_partial = float(scalp.get("partial_pct", 50) or 50)
        self.scalp_lock = bool(scalp.get("lock", True))
        lock = risk_cfg.get("lock_profit", {})
        self.lock_enabled = bool(lock.get("enabled", False))
        self.lock_arm_pct = float(lock.get("arm_pct", 0.8))
        self.lock_trail_pct = float(lock.get("trail_pct", 0.5))
        self.lock_breakeven_pct = float(lock.get("breakeven_pct", 0.4))
        self.halted = False
        self.halt_reason = ""
        self._day = ist_now().date().isoformat()
        self._day_start_equity: Optional[float] = None

    def update_capital(self, balance: float):
        """Rebase the risk envelope (used when trading LIVE against the real
        DHAN account balance instead of the configured paper capital)."""
        if balance and balance > 0:
            self.initial_capital = float(balance)
            log.info("risk capital updated to %.0f (live balance)", balance)

    # ---------- day plumbing ----------
    def _day_str(self) -> str:
        d = self._clock().date().isoformat()
        if d != self._day:
            self._day = d
            self._day_start_equity = None
            if self.halted and "daily" not in self.halt_reason:
                pass
            # a new trading day clears yesterday's halt (loss/profit/streak limits)
            self.halted = False
            self.halt_reason = ""
        return d

    def set_day_start_equity(self, equity: float):
        if self._day_start_equity is None:
            self._day_start_equity = equity

    # ---------- queries ----------
    def day_stats(self) -> dict:
        day = self._day_str()
        trades = self.store.trades_today(day)
        pnl = sum(t["pnl"] for t in trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        return {"day": day, "trades": len(trades), "pnl": pnl,
                "wins": wins, "losses": len(trades) - wins}

    def open_position_count(self, open_positions: int) -> int:
        return open_positions

    def exposure_ok(self, premium_value: float, open_exposure: float) -> bool:
        limit = self.initial_capital * self.max_exposure_pct / 100.0
        return open_exposure + premium_value <= limit

    def drawdown_ok(self, current_equity: float) -> bool:
        if self._day_start_equity is None or self._day_start_equity <= 0:
            return True
        dd = (self._day_start_equity - current_equity) / self._day_start_equity * 100.0
        return dd <= self.max_drawdown_pct

    # ---------- gate ----------
    def check_entry(self, open_positions: int, open_exposure: float, current_equity: float,
                    premium_value: float, now_hm: int | None = None) -> tuple[bool, str]:
        day = self._day_str()
        if self.halted:
            return False, "halted: %s" % self.halt_reason
        stats = self.day_stats()
        if stats["trades"] >= self.max_daily_trades:
            return False, "max daily trades (%d) reached" % self.max_daily_trades
        if self.max_consecutive_losses > 0 and stats["trades"] >= self.max_consecutive_losses:
            streaks = 0
            for t in reversed(self.store.trades_today(day)):
                if t["pnl"] <= 0:
                    streaks += 1
                else:
                    break
            if streaks >= self.max_consecutive_losses:
                return False, "consecutive losses (%d) - halting new entries for the day" % streaks
        if stats["pnl"] <= -self.daily_loss_limit:
            self.halted = True
            self.halt_reason = "daily loss limit hit (%.0f)" % stats["pnl"]
            return False, self.halt_reason
        if self.daily_profit_target > 0 and stats["pnl"] >= self.daily_profit_target:
            return False, "daily profit target reached (%.0f)" % stats["pnl"]
        if not self.exposure_ok(premium_value, open_exposure):
            return False, "exposure limit would be exceeded"
        if open_positions >= self.max_positions:
            return False, "max positions (%d) reached" % self.max_positions
        if not self.drawdown_ok(current_equity):
            self.halted = True
            self.halt_reason = "drawdown > %.1f%%" % self.max_drawdown_pct
            return False, self.halt_reason
        if now_hm is None:
            now_hm = self._clock().hour * 100 + self._clock().minute
        if now_hm > self.no_new_after:
            return False, "after no-new-entries time %04d" % self.no_new_after
        return True, "ok"

    # ---------- sizing ----------
    def size_position(self, premium_entry: float, underlying: str, lot_size: int = 0,
                      sl_pct: float | None = None) -> int:
        """Return quantity in units (multiple of lot size) for a long option."""
        sl_pct = sl_pct or self.sl_pct
        if premium_entry <= 0:
            return 0
        loss_per_unit = premium_entry * sl_pct
        qty = int(self.risk_per_trade / loss_per_unit)
        ls = lot_size or self.lot_sizes.get(underlying, 1) or 1
        lots = max(1, qty // ls)
        # cap premium value at per-position share of max exposure
        cap_value = self.initial_capital * self.max_exposure_pct / 100.0 / max(1, self.max_positions)
        max_qty_by_value = int(cap_value / premium_entry)
        qty = min(lots * ls, max_qty_by_value)
        # never round back up past the cap: floor to a full lot, or 0 if a lot doesn't fit
        qty = (qty // ls) * ls
        return qty if qty >= ls else 0

    def sl_target_prices(self, premium_entry: float, sl_pct: float | None = None):
        sl_pct = sl_pct or self.sl_pct
        sl = premium_entry * (1 - sl_pct)
        tp = premium_entry + premium_entry * sl_pct * self.reward_risk
        return sl, tp

    def r_value(self, premium_entry: float, sl_price: float) -> float:
        """One R (entry risk in premium points) for a long option."""
        return max(0.0, premium_entry - sl_price)

    def exit_target_price(self, premium_entry: float, sl_price: float) -> float:
        """Backstop target for trail/reversal modes: entry + target_mult_r x R."""
        r = self.r_value(premium_entry, sl_price)
        return premium_entry + r * self.target_mult_r

    def record_equity(self, cash: float, unrealized: float):
        day = self._day_str()
        equity = cash + unrealized
        stats = self.day_stats()
        day_pnl = stats["pnl"] + unrealized
        if self._day_start_equity is None:
            self._day_start_equity = equity
        dd = 0.0
        if self._day_start_equity > 0:
            dd = (self._day_start_equity - equity) / self._day_start_equity * 100.0
        self.store.record_equity(day, cash, unrealized, equity, day_pnl, dd)
        return equity, dd
