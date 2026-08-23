"""Instrument master, expiry resolution and option symbol helpers for DHAN.

The option-chain API returns a security_id per strike, so that is the primary
source for order security ids. The scrip master CSV is used to resolve index
security ids, lot sizes and to double-check symbols.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "scrip_master.csv"

OPTION_SYMBOL_RE = re.compile(
    r"^(?P<underlying>[A-Z0-9 ]+?)\s+(?P<dd>\d{1,2})\s+(?P<mon>[A-Z]{3})\s+(?P<strike>\d+)\s+(?P<ot>CE|PE)$"
)
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


@dataclass
class Underlying:
    name: str                 # NIFTY / BANKNIFTY
    security_id: str          # F&O underlying id used by the option-chain API (NIFTY=26000, BANKNIFTY=26009)
    index_id: str = ""        # index id used by quotes/history/WS (NIFTY=13, BANKNIFTY=25; from scrip master)
    segment: str = "NSE_FNO"  # segment used by option-chain / expiry-list APIs
    expiry: str = "nearest"
    lot_size: int = 0
    strike_interval: int = 50
    fno_segment: str = "NSE_FNO"


@dataclass
class OptionContract:
    security_id: str
    symbol: str
    underlying: str
    expiry: str              # YYYY-MM-DD
    strike: float
    option_type: str         # CE / PE
    lot_size: int = 0


class InstrumentMaster:
    """Downloads/loads the DHAN scrip master and resolves key ids + lot sizes."""

    def __init__(self, cache_path: Path | None = None, timeout: int = 30):
        self.cache_path = cache_path or CACHE_PATH
        self.timeout = timeout
        self.df: Optional[pd.DataFrame] = None
        self._index_ids: dict[str, str] = {}
        self._lot_sizes: dict[str, int] = {}

    def load(self, refresh: bool = False) -> bool:
        if self.df is not None and not refresh:
            return True
        if not refresh and self.cache_path.exists():
            try:
                self.df = pd.read_csv(self.cache_path, low_memory=False)
                log.info("scrip master loaded from cache (%d rows)", len(self.df))
                self._index()
                return True
            except Exception as e:  # pragma: no cover
                log.warning("cache read failed: %s", e)
        try:
            r = requests.get(SCRIP_MASTER_URL, timeout=self.timeout)
            r.raise_for_status()
            self.df = pd.read_csv(io.StringIO(r.text), low_memory=False)
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.df.to_csv(self.cache_path, index=False)
            log.info("scrip master downloaded (%d rows)", len(self.df))
            self._index()
            return True
        except Exception as e:
            log.warning("scrip master download failed: %s", e)
            return False

    def _index(self):
        """Dhan scrip master segments: I = indices, E = equities, D = derivatives."""
        if self.df is None:
            return
        cols = {c: c for c in self.df.columns}
        seg = cols.get("SEM_SEGMENT")
        sym = cols.get("SEM_TRADING_SYMBOL")
        sid = cols.get("SEM_SMST_SECURITY_ID")
        if seg is None or sym is None or sid is None:
            return
        # indices
        idx = self.df[self.df[seg] == "I"]
        for _, row in idx.iterrows():
            self._index_ids[str(row[sym]).strip().upper()] = str(row[sid])
        # option lot sizes: OPTIDX rows, underlying = symbol before the first "-"
        opt = self.df[(self.df[seg] == "D") & (self.df[cols.get("SEM_INSTRUMENT_NAME", "SEM_INSTRUMENT_NAME")] == "OPTIDX")]
        lot = cols.get("SEM_LOT_UNITS") or cols.get("SEM_LOT_SIZE")
        if lot:
            for _, row in opt.iterrows():
                try:
                    u = str(row[sym]).split("-")[0].strip().upper()
                    ls = int(float(row[lot]))
                except (TypeError, ValueError):
                    continue
                if u and (u not in self._lot_sizes or ls > 0):
                    self._lot_sizes[u] = ls

    def index_security_id(self, name: str) -> str | None:
        return self._index_ids.get(name.strip().upper())

    def lot_size(self, underlying: str) -> int | None:
        return self._lot_sizes.get(underlying.strip().upper())

    def stock_security_id(self, symbol: str) -> str | None:
        """Resolve an NSE cash-equity security_id from the scrip master (segment E)."""
        if self.df is None:
            return None
        cols = {c: c for c in self.df.columns}
        sym = cols.get("SEM_TRADING_SYMBOL")
        sid = cols.get("SEM_SMST_SECURITY_ID")
        seg = cols.get("SEM_SEGMENT")
        if not sym or not sid:
            return None
        m = self.df[(self.df[sym].astype(str).str.upper() == symbol.upper()) &
                    (self.df[seg] == "E")]
        if len(m):
            return str(m.iloc[0][sid])
        return None

    def option_security_id(self, symbol: str) -> str | None:
        """Resolve security_id for a NSE option trading symbol from the master."""
        if self.df is None:
            return None
        cols = {c: c for c in self.df.columns}
        sym = cols.get("SEM_TRADING_SYMBOL")
        sid = cols.get("SEM_SMST_SECURITY_ID")
        if not sym or not sid:
            return None
        m = self.df[self.df[sym].astype(str).str.upper() == symbol.upper()]
        if len(m):
            return str(m.iloc[0][sid])
        return None


def parse_option_symbol(symbol: str):
    """'NIFTY 25 SEP 24500 CE' -> (underlying, expiry_date, strike, option_type) or None."""
    m = OPTION_SYMBOL_RE.match(symbol.strip().upper())
    if not m:
        return None
    year = datetime.now().year
    mon = MONTHS.get(m.group("mon"))
    if mon is None:
        return None
    expiry = date(year, mon, int(m.group("dd")))
    # handle year rollover (expiry in past => next year)
    if expiry < date.today():
        expiry = date(expiry.year + 1, expiry.month, expiry.day)
    return m.group("underlying").strip(), expiry.isoformat(), float(m.group("strike")), m.group("ot")


def nearest_expiry(expiries: list[str], today: date | None = None) -> str | None:
    today = today or date.today()
    future = sorted(e for e in expiries if e >= today.isoformat())
    return future[0] if future else None


def expiry_by_rank(expiries: list[str], rank: str = "nearest", today: date | None = None) -> str | None:
    today = today or date.today()
    future = sorted(e for e in expiries if e >= today.isoformat())
    if not future:
        return None
    if rank == "next":
        return future[1] if len(future) > 1 else future[0]
    return future[0]


def select_expiry(expiries: list[str], policy: str = "dte_window", min_dte: float = 0.1,
                  prefer_min: float = 2.0, prefer_max: float = 5.0, now=None) -> str | None:
    """Choose the best tradable expiry.

    - nearest    : first future expiry (old behaviour)
    - dte_window : nearest expiry that is NOT in its theta-trap (dte >= min_dte),
                   preferring one 2-5 days out; falls back to the nearest future one.
    """
    future = sorted(e for e in expiries if dte(e, now) > 0)
    if not future:
        return None
    if policy == "nearest":
        return future[0]
    best = None
    for e in future:
        d = dte(e, now)
        if prefer_min <= d <= prefer_max:
            return e
        if d >= min_dte and best is None:
            best = e
    return best or future[0]


def expiry_with_dte(expiries: list[str], now=None) -> list[dict]:
    """[(expiry, dte)] sorted by expiry, with dte measured to the 15:30 IST close."""
    out = []
    for e in sorted(expiries):
        out.append({"expiry": e, "dte": round(dte(e, now), 2)})
    return out


def atm_strike(spot: float, strike_interval: float) -> float:
    return round(spot / strike_interval) * strike_interval


def strike_ladder(atm: float, interval: float, width: int = 8) -> list[float]:
    """Return [atm-width*interval .. atm+width*interval] step interval."""
    return [atm + i * interval for i in range(-width, width + 1)]


def dte(expiry_iso: str, now=None) -> float:
    """Fractional days to expiry, measured to the 15:30 IST close.

    1.0 = a full trading day away; 0.1 = ~2.4 hours before expiry close."""
    from datetime import datetime, time as dtime, timedelta
    import zoneinfo
    if now is None:
        now = datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata"))
    try:
        exp = date.fromisoformat(expiry_iso)
    except ValueError:
        return 999.0
    exp_dt = datetime.combine(exp, dtime(15, 30), tzinfo=now.tzinfo)
    return (exp_dt - now).total_seconds() / 86400.0
