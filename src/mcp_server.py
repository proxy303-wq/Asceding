"""MCP server for the portfolio manager.

Exposes read + control tools over the Model Context Protocol so any MCP client
(Claude Desktop/Code, Cursor, Codex, DSH...) can query the portfolio, inspect
signals/risk, and queue paper trades or pause auto-trading.

Run:  python -m src.mcp_server            (stdio transport)
The loop must be running for paper_trade / set_auto_trading to take effect.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import FastMCP  # noqa: E402

from src import bridge  # noqa: E402
from src.config import load_config  # noqa: E402
from src.db.store import Store  # noqa: E402

mcp = FastMCP("dhan-auto-trader")

_cfg = load_config()
_store = Store(_cfg.get("db_path", "data/trader.db"))


def _state() -> dict:
    return bridge.read_state()


@mcp.tool()
def get_portfolio_summary() -> dict:
    """Portfolio summary: mode, cash, equity, today's P&L, trade count, win rate."""
    s = _state()
    return {
        "mode": s.get("mode", "unknown"),
        "market_open": s.get("market_open", False),
        "auto_trading": s.get("auto", True),
        "cash": s.get("cash"),
        "equity": s.get("equity"),
        "day_pnl": s.get("day_pnl"),
        "trades_today": s.get("trades_today"),
        "win_rate_today_pct": s.get("win_rate_today"),
        "halted": s.get("halted"),
        "halt_reason": s.get("halt_reason"),
        "updated_ts": s.get("ts"),
    }


@mcp.tool()
def get_open_positions() -> list[dict]:
    """Currently open option positions with entry/SL/target and unrealized P&L."""
    return _state().get("open_positions", [])


@mcp.tool()
def get_risk_status() -> dict:
    """Risk limits and whether new entries are allowed."""
    s = _state()
    limits = s.get("limits", {})
    return {
        "halted": s.get("halted", False),
        "halt_reason": s.get("halt_reason", ""),
        "limits": limits,
        "open_positions_count": len(s.get("open_positions", [])),
    }


@mcp.tool()
def get_recent_signals(limit: int = 20) -> list[dict]:
    """Most recent strategy signals (entry suggestions)."""
    return _store.recent_signals(limit)


@mcp.tool()
def get_trade_log(limit: int = 50) -> list[dict]:
    """Closed trades with P&L and exit reason."""
    return _store.all_trades(limit)


@mcp.tool()
def get_equity_curve(limit: int = 100) -> list[dict]:
    """Equity curve snapshots (ts, equity, day_pnl, drawdown)."""
    return _state().get("equity_curve", [])[-limit:]


@mcp.tool()
def get_strategy_config() -> dict:
    """Which strategies are enabled and their key parameters."""
    cfg = load_config()
    out = {}
    for name, s in cfg.get("strategies", {}).items():
        out[name] = {"enabled": s.get("enabled", False),
                     **{k: v for k, v in s.items() if k != "enabled"}}
    return out


@mcp.tool()
def get_expiries(underlying: str = "NIFTY") -> dict:
    """Available expiries for an underlying with days-to-expiry, ATM IV and which
    one the trader is currently selecting. Underlying: NIFTY or BANKNIFTY."""
    s = _state()
    exps = s.get("expiries", {}).get(underlying.upper(), [])
    if not exps:
        return {"ok": False, "message": "no expiry data yet - is the trading loop running?"}
    return {"ok": True, "underlying": underlying.upper(), "expiries": exps}


@mcp.tool()
def get_option_chain(underlying: str = "NIFTY", width: int = 3) -> dict:
    """Latest ATM+/-width option chain with Greeks and theta-per-day % (for choosing
    strikes that bleed the least time value). Underlying: NIFTY or BANKNIFTY."""
    s = _state()
    chains = s.get("chains", {})
    ladder = chains.get(underlying.upper(), [])
    if not ladder:
        return {"ok": False, "message": "no chain snapshot yet - is the trading loop running?"}
    out = []
    for r in ladder[: 2 * width + 1]:
        row = {"strike": r["strike"], "itm_delta_strikes": r.get("itm_delta")}
        for ot in ("CE", "PE"):
            c = r.get(ot)
            if c:
                row[ot] = f"ltp={c['ltp']} iv={c['iv']:.3f} delta={c['delta']:.2f} "                           f"theta={c['theta']:.2f}/day ({c['theta_pct']}% of premium)"
        out.append(row)
    return {"ok": True, "underlying": underlying.upper(), "ladder": out}


@mcp.tool()
def set_kill_switch(activate: bool) -> dict:
    """Arm or disarm the Dhan exchange-level kill switch (live mode only).
    Activation requires all positions closed and no pending orders; it blocks
    all trading for the rest of the session."""
    bridge.set_control({"kill_switch_requested": activate})
    return {"ok": True, "requested": activate,
            "message": "kill-switch request queued for the trading loop"}


@mcp.tool()
def set_trading_mode(mode: str) -> dict:
    """Switch between paper and live execution. LIVE places real orders on your
    DHAN account balance; the switch applies once all open positions are flat.
    mode: 'paper' or 'live'."""
    mode = mode.strip().lower()
    if mode not in ("paper", "live"):
        return {"ok": False, "message": "mode must be 'paper' or 'live'"}
    bridge.set_control({"mode": mode})
    return {"ok": True, "mode": mode,
            "message": f"switch to {mode.upper()} queued (applies when no open positions)"}


@mcp.tool()
def set_auto_trading(enabled: bool) -> dict:
    """Pause or resume signal-driven auto-trading (manual orders still allowed)."""
    bridge.set_control({"auto": bool(enabled)})
    return {"ok": True, "auto": bool(enabled)}


@mcp.tool()
def paper_trade(underlying: str, option_type: str, strike: float, side: str = "BUY",
                quantity: int = 0, sl_pct: float | None = None, expiry: str = "") -> dict:
    """Queue a manual paper order for the running loop to execute.

    underlying: NIFTY or BANKNIFTY. option_type: CE or PE. side: BUY or SELL.
    quantity: 0 = auto-size by risk rules. sl_pct: stop as % of premium.
    """
    order = {
        "underlying": underlying.upper(), "option_type": option_type.upper(),
        "strike": float(strike), "side": side.upper(), "qty": int(quantity),
        "sl_pct": sl_pct, "expiry": expiry,
    }
    if order["side"] not in ("BUY", "SELL") or order["option_type"] not in ("CE", "PE"):
        return {"ok": False, "message": "invalid side/option_type"}
    order = bridge.append_manual_order(order)
    return {"ok": True, "order": order,
            "message": "queued for the trading loop; check state.json for outcome"}


if __name__ == "__main__":
    mcp.run(transport="stdio")
