"""Option-chain service: fetch, enrich with our Greeks, track IV percentile."""
from __future__ import annotations

import json
import logging
import math
from datetime import date
from pathlib import Path
from typing import Optional

from ..analytics import greeks as g
from ..broker.base import ChainSnapshot, OptionRow
from ..broker.dhan_live import DhanClient
from ..config import ROOT

log = logging.getLogger(__name__)

RISK_FREE_RATE = 0.065


class ChainService:
    def __init__(self, data: DhanClient, strike_intervals: dict[str, float] | None = None,
                 iv_history_path: str | None = None):
        self.data = data
        self.strike_intervals = strike_intervals or {}
        self.iv_history_path = Path(iv_history_path or (ROOT / "data" / "iv_history.json"))
        self._cache: dict[str, ChainSnapshot] = {}
        self._last_fetch: dict[str, float] = {}
        self.expiry_lists: dict[str, list[str]] = {}
        self._last_expiry_fetch: dict[str, float] = {}
        self.selected_expiry: dict[str, str] = {}

    def refresh_expiries(self, underlying: str, security_id: str, seg: str,
                          min_gap_s: float = 60.0) -> list[str]:
        """Fetch and cache the expiry list for an underlying (rate-limited)."""
        now = __import__("time").time()
        if underlying in self._last_expiry_fetch and now - self._last_expiry_fetch[underlying] < min_gap_s:
            return self.expiry_lists.get(underlying, [])
        try:
            self.expiry_lists[underlying] = self.data.expiry_list(security_id, seg)
            self._last_expiry_fetch[underlying] = now
        except Exception as e:
            log.warning("expiry list failed for %s: %s", underlying, e)
        return self.expiry_lists.get(underlying, [])

    def fetch(self, underlying: str, security_id: str, seg: str, expiry: str,
              spot_override: float = 0.0, min_gap_s: float = 3.0) -> Optional[ChainSnapshot]:
        key = f"{underlying}:{expiry}"
        now = __import__("time").time()
        if key in self._last_fetch and now - self._last_fetch[key] < min_gap_s:
            return self._cache.get(key)
        try:
            snap = self.data.option_chain(security_id, seg, expiry)
        except Exception as e:
            log.warning("chain fetch failed for %s: %s", key, e)
            return self._cache.get(key)
        if snap.spot <= 0 and spot_override > 0:
            snap.spot = spot_override
        self._enrich(snap, expiry)
        self._cache[key] = snap
        self._last_fetch[key] = now
        self._record_iv(snap, underlying)
        return snap

    def _enrich(self, snap: ChainSnapshot, expiry: str):
        interval = self.strike_intervals.get(snap.underlying, 50.0)
        dte_days = max(0.0, (date.fromisoformat(expiry) - date.today()).days) if expiry else 7.0
        T = dte_days / 365.0
        atm_iv = snap.iv_atm(interval)
        for (strike, ot), row in snap.rows.items():
            if T <= 0:
                continue
            try:
                if row.iv <= 0 and atm_iv > 0:
                    row.iv = atm_iv
                if row.iv > 0 and row.ltp > 0:
                    gr = g.bs_greeks(ot[0], snap.spot, strike, T, RISK_FREE_RATE, row.iv)
                    if row.delta == 0:
                        row.delta = gr["delta"]
                    if row.gamma == 0:
                        row.gamma = gr["gamma"]
                    if row.theta == 0:
                        row.theta = gr["theta"]
                    if row.vega == 0:
                        row.vega = gr["vega"]
                elif row.ltp > 0 and snap.spot > 0:
                    iv = g.implied_vol(ot[0], snap.spot, strike, T, RISK_FREE_RATE, row.ltp)
                    if iv == iv and 0 < iv < 3:
                        row.iv = iv
                        gr = g.bs_greeks(ot[0], snap.spot, strike, T, RISK_FREE_RATE, iv)
                        row.delta, row.gamma = gr["delta"], gr["gamma"]
                        row.theta, row.vega = gr["theta"], gr["vega"]
            except Exception:
                continue

    def _record_iv(self, snap: ChainSnapshot, underlying: str):
        interval = self.strike_intervals.get(underlying, 50.0)
        iv = snap.iv_atm(interval)
        if iv > 0:
            hist = self._load_iv_history()
            hist.setdefault(underlying, {})
            hist[underlying][date.today().isoformat()] = iv
            # keep last 120 sessions
            keep = sorted(hist[underlying].items())[-120:]
            hist[underlying] = dict(keep)
            self._save_iv_history(hist)

    def iv_percentile(self, underlying: str) -> float:
        hist = self._load_iv_history().get(underlying, {})
        values = [v for v in hist.values() if v > 0]
        if not values:
            return 50.0
        today = date.today().isoformat()
        current = hist.get(today, values[-1])
        below = sum(1 for v in values if v <= current)
        return 100.0 * below / len(values)

    def _load_iv_history(self) -> dict:
        if self.iv_history_path.exists():
            try:
                return json.loads(self.iv_history_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_iv_history(self, hist: dict):
        self.iv_history_path.parent.mkdir(parents=True, exist_ok=True)
        self.iv_history_path.write_text(json.dumps(hist), encoding="utf-8")
