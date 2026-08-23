"""Strike selection: pick the best ATM/ITM option to minimise time decay.

The theta_optimized mode scores candidate strikes (ATM +/- width) so we prefer
in-the-money options whose theta bleed is small relative to premium, while keeping
delta in a usable band and avoiding illiquid strikes. Fixed mode keeps the
strategy's chosen strike.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..broker.base import ChainSnapshot, OptionRow

log = logging.getLogger(__name__)


def theta_pct_per_day(row: OptionRow) -> float:
    """Theta bleed as % of premium per day (0 if unknown)."""
    if row.theta < 0 and row.ltp > 0:
        return -row.theta / row.ltp * 100.0
    return 0.0


def _score(row: OptionRow, cfg: dict) -> float:
    delta_min = float(cfg.get("delta_min", 0.30))
    delta_max = float(cfg.get("delta_max", 0.70))
    theta_max = float(cfg.get("theta_max_pct", 2.0))
    tp = theta_pct_per_day(row)
    delta = abs(row.delta)
    # primary: theta bleed per day (normalized)
    theta_s = tp / max(theta_max, 0.5)
    # delta band penalty: distance outside [delta_min, delta_max] (weighted 2x - ITM drift is costly)
    if delta_min <= delta <= delta_max:
        delta_s = 0.0
    else:
        delta_s = min(abs(delta - delta_min), abs(delta - delta_max)) / max(delta_min, 0.1) * 2.0
    # premium cost penalty: deep-ITM options cost too much for the theta saved
    premium_s = 0.0
    atm = float(cfg.get("_atm_premium", 0.0))
    if atm > 0 and row.ltp > 0:
        premium_s = max(0.0, row.ltp / atm - 1.5) * 1.5
    # liquidity penalty: prefer higher OI + volume
    liq = row.oi + row.volume
    liq_s = 0.0 if liq >= 50000 else (1.0 - liq / 50000.0) * 0.3
    return theta_s + delta_s + premium_s + liq_s


def select_best_strike(snap: ChainSnapshot, option_type: str, interval: float,
                       cfg: dict, hint_strike: Optional[float] = None) -> Optional[OptionRow]:
    """Return the best OptionRow for option_type, or None if nothing usable."""
    mode = cfg.get("mode", "theta_optimized")
    width = int(cfg.get("width", 3))
    atm = snap.atm_strike(interval)
    if mode == "fixed":
        k = hint_strike if hint_strike else atm
        return snap.get(k, option_type)
    # reference ATM premium so the selector never drifts into unaffordable ITM strikes
    atm_row = snap.get(atm, option_type)
    cfg = dict(cfg)
    cfg["_atm_premium"] = atm_row.ltp if atm_row else 0.0
    strikes = [atm + i * interval for i in range(-width, width + 1)]
    best, best_score = None, None
    for k in strikes:
        row = snap.get(k, option_type)
        if row is None or row.ltp <= 0:
            continue
        if (row.oi + row.volume) <= 0:
            continue
        if cfg.get("_atm_premium", 0.0) > 0 and row.ltp > cfg["_atm_premium"] * 2.5:
            continue                      # too deep ITM: premium out of budget
        s = _score(row, cfg)
        if best is None or s < best_score:
            best, best_score = row, s
    if best is None and hint_strike:
        return snap.get(hint_strike, option_type)
    return best


def chain_ladder(snap: ChainSnapshot, interval: float, width: int = 3) -> list[dict]:
    """ATM +/- width ladder with greeks + theta% for observation (state/dashboard/MCP)."""
    atm = snap.atm_strike(interval)
    out = []
    for i in range(-width, width + 1):
        k = atm + i * interval
        ce = snap.get(k, "CE")
        pe = snap.get(k, "PE")
        entry = {"strike": k, "itm_delta": round((k - snap.spot) / interval, 1)}
        for ot, row in (("CE", ce), ("PE", pe)):
            if row and row.ltp > 0:
                entry[ot] = {
                    "ltp": round(row.ltp, 2), "iv": round(row.iv, 4),
                    "delta": round(row.delta, 3), "theta": round(row.theta, 2),
                    "theta_pct": round(theta_pct_per_day(row), 2),
                    "oi": int(row.oi), "volume": int(row.volume),
                }
            else:
                entry[ot] = None
        out.append(entry)
    return out
