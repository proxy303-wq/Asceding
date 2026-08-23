"""SQLite persistence: signals, trades, equity curve, app state."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


class Store:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self):
        c = self.conn
        c.execute("""CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, strategy TEXT, underlying TEXT, direction TEXT,
            strike REAL, option_type TEXT, expiry TEXT, reason TEXT,
            meta TEXT, acted INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, security_id TEXT, symbol TEXT, underlying TEXT,
            option_type TEXT, strike REAL, expiry TEXT, strategy TEXT,
            entry_price REAL, exit_price REAL, quantity INTEGER,
            pnl REAL, exit_reason TEXT, meta TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS equity (
            ts REAL, day TEXT, cash REAL, unrealized REAL, equity REAL,
            day_pnl REAL, drawdown_pct REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS app_state (
            k TEXT PRIMARY KEY, v TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS iv_history (
            day TEXT, underlying TEXT, iv_atm REAL, PRIMARY KEY(day, underlying))""")
        c.execute("""CREATE TABLE IF NOT EXISTS ml_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, strategy TEXT, underlying TEXT,
            features TEXT, label INTEGER, outcome REAL, exit_reason TEXT)""")
        self.conn.commit()

    # ---------- signals ----------
    def record_signal(self, strategy: str, underlying: str, direction: str, strike: float,
                      option_type: str, expiry: str, reason: str, meta: dict = None):
        cur = self.conn.execute(
            "INSERT INTO signals (ts, strategy, underlying, direction, strike, option_type, expiry, reason, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), strategy, underlying, direction, strike, option_type, expiry,
             reason, json.dumps(meta or {})),
        )
        self.conn.commit()
        return cur.lastrowid

    def mark_signal_acted(self, sig_id: int):
        self.conn.execute("UPDATE signals SET acted=1 WHERE id=?", (sig_id,))
        self.conn.commit()

    def recent_signals(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- trades ----------
    def record_trade(self, security_id: str, symbol: str, underlying: str, option_type: str,
                     strike: float, expiry: str, strategy: str, entry_price: float,
                     exit_price: float, quantity: int, pnl: float, exit_reason: str,
                     meta: dict = None, ts: float | None = None):
        self.conn.execute(
            "INSERT INTO trades (ts, security_id, symbol, underlying, option_type, strike, expiry,"
            " strategy, entry_price, exit_price, quantity, pnl, exit_reason, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts if ts is not None else time.time(), security_id, symbol, underlying,
             option_type, strike, expiry, strategy, entry_price, exit_price, quantity,
             pnl, exit_reason, json.dumps(meta or {})),
        )
        self.conn.commit()

    def trades_today(self, day: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE date(ts, 'unixepoch', 'localtime')=? ORDER BY ts", (day,)
        ).fetchall()
        return [dict(r) for r in rows]

    def all_trades(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def day_pnl(self, day: str) -> float:
        rows = self.conn.execute(
            "SELECT pnl FROM trades WHERE date(ts, 'unixepoch', 'localtime')=?", (day,)
        ).fetchall()
        return sum(r["pnl"] for r in rows)

    # ---------- equity ----------
    def record_equity(self, day: str, cash: float, unrealized: float, equity: float,
                      day_pnl: float, drawdown_pct: float):
        self.conn.execute(
            "INSERT INTO equity (ts, day, cash, unrealized, equity, day_pnl, drawdown_pct)"
            " VALUES (?,?,?,?,?,?,?)",
            (time.time(), day, cash, unrealized, equity, day_pnl, drawdown_pct),
        )
        self.conn.commit()

    def equity_curve(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows][::-1]

    # ---------- ml samples ----------
    def record_ml_sample(self, strategy: str, underlying: str, features: list,
                         label: int | None = None, outcome: float | None = None,
                         ts: float | None = None, exit_reason: str | None = None):
        self.conn.execute(
            "INSERT INTO ml_samples (ts, strategy, underlying, features, label, outcome, exit_reason)"
            " VALUES (?,?,?,?,?,?,?)",
            (ts if ts is not None else time.time(), strategy, underlying,
             json.dumps(features), label, outcome, exit_reason),
        )
        self.conn.commit()

    def ml_samples(self, labeled_only: bool = True) -> list[dict]:
        if labeled_only:
            rows = self.conn.execute(
                "SELECT * FROM ml_samples WHERE label IS NOT NULL ORDER BY ts").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM ml_samples ORDER BY ts").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["features"] = json.loads(d["features"])
            except (json.JSONDecodeError, TypeError):
                d["features"] = []
            out.append(d)
        return out

    # ---------- iv history ----------
    def record_iv(self, day: str, underlying: str, iv_atm: float):
        self.conn.execute(
            "INSERT OR REPLACE INTO iv_history (day, underlying, iv_atm) VALUES (?,?,?)",
            (day, underlying, iv_atm),
        )
        self.conn.commit()

    def iv_history(self, underlying: str, limit: int = 90) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM iv_history WHERE underlying=? ORDER BY day DESC LIMIT ?", (underlying, limit)
        ).fetchall()
        return [dict(r) for r in rows][::-1]

    # ---------- app state ----------
    def set_state(self, k: str, v: Any):
        self.conn.execute("INSERT OR REPLACE INTO app_state (k, v) VALUES (?,?)", (k, json.dumps(v)))
        self.conn.commit()

    def get_state(self, k: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM app_state WHERE k=?", (k,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["v"])
        except (json.JSONDecodeError, TypeError):
            return row["v"]
