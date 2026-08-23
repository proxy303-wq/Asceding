"""Streamlit dashboard for dhan-auto-trader.

Hosts the same live view as the FastAPI dashboard but in Streamlit (easier to
deploy). Reads the loop's state.json + SQLite, so run the trading loop first:
    python scripts/paper_run.py
Then start this app:
    streamlit run src/dashboard/streamlit_app.py --server.port 8501
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src import bridge  # noqa: E402
from src.config import load_config  # noqa: E402
from src.db.store import Store  # noqa: E402

st.set_page_config(page_title="dhan-auto-trader", layout="wide", page_icon="📈")

_cfg = load_config()
_store = Store(_cfg.get("db_path", "data/trader.db"))


def load_state() -> dict:
    s = bridge.read_state()
    if not s:
        s = {"running": False, "mode": _cfg.get("mode", "paper")}
    else:
        s["running"] = True
    return s


def main():
    s = load_state()
    running = s.get("running", False)

    st.title("📈 dhan-auto-trader · portfolio manager")
    if not running:
        st.warning("Trading loop is not running. Start it with:  python scripts/paper_run.py")
        st.stop()

    # ---------------- header / controls ----------------
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Equity (₹)", f"{s.get('equity', 0):,.0f}")
    with c2:
        pnl = s.get("day_pnl", 0)
        st.metric("Day P&L (₹)", f"{pnl:+,.0f}", delta=f"{pnl:+,.0f}")
    with c3:
        st.metric("Trades today", f"{s.get('trades_today', 0)}",
                  help=f"win rate {s.get('win_rate_today', 0)}%")

    with st.sidebar:
        st.header("Controls")
        auto = st.toggle("Auto-trading", value=bool(s.get("auto", True)))
        if auto != bool(s.get("auto", True)):
            bridge.set_control({"auto": auto})
            st.toast("auto-trading " + ("ON" if auto else "PAUSED"))

        current = s.get("mode", "paper")
        st.write("Trading mode: **" + current.upper() + "**")
        if current == "live":
            st.error("LIVE mode - real orders on your DHAN balance")
        else:
            st.info("PAPER mode - simulated")
        want_live = st.radio("Switch to:", ["paper", "live"],
                             index=0 if current == "paper" else 1,
                             label_visibility="collapsed")
        confirm = False
        if want_live != current:
            if want_live == "live":
                confirm = st.checkbox("I understand LIVE places real orders on my Dhan account")
            else:
                confirm = True
            if confirm and st.button("Apply mode switch"):
                bridge.set_control({"mode": want_live})
                st.toast(f"mode switch to {want_live.upper()} queued (applies when flat)")
                st.rerun()

        st.divider()
        st.caption("Market: " + ("open" if s.get("market_open") else "closed"))
        if s.get("halted"):
            st.error("HALTED: " + str(s.get("halt_reason", "")))
        st.caption("Last update: " + time.strftime("%H:%M:%S", time.localtime(s.get("ts", 0))))

    # ---------------- equity curve ----------------
    st.subheader("Equity curve")
    eq = s.get("equity_curve", [])
    if len(eq) > 2:
        df = pd.DataFrame([{"ts": e["ts"], "equity": e["equity"]} for e in eq])
        st.line_chart(df.set_index("ts"))
    else:
        st.caption("not enough equity snapshots yet")

    # ---------------- positions ----------------
    st.subheader(f"Open positions ({len(s.get('open_positions', []))})")
    pos = s.get("open_positions", [])
    if pos:
        rows = []
        for p in pos:
            g = p.get("greeks", {})
            rows.append({
                "symbol": p["symbol"] + (" " + str(p["strike"]) if p.get("strike") else ""),
                "side": p["side"], "qty": p["qty"],
                "entry": p.get("entry_price", 0), "sl": p.get("sl_price", 0),
                "target": p.get("target_price", 0),
                "Δ": g.get("delta", 0), "θ%": round(g.get("theta", 0) / p.get("entry_price", 1) * 100, 2) if p.get("entry_price") else 0,
                "unrealized": round(p.get("unrealized", 0), 0),
                "flags": ("BTST " if p.get("btst") else "") + ("STOCK " if p.get("segment") == "EQ" else ""),
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.caption("no open positions")

    # ---------------- expiries + chain ----------------
    exps = s.get("expiries", {})
    if exps:
        with st.expander("Expiries (DTE / IV)", expanded=False):
            for u, rows in exps.items():
                st.markdown(f"**{u}**")
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    chains = s.get("chains", {})
    if chains:
        with st.expander("Option chain ATM±3 (θ% per day - prefer low)", expanded=False):
            for u, ladder in chains.items():
                st.markdown(f"**{u}**")
                rows = []
                for r in ladder:
                    for ot in ("CE", "PE"):
                        c = r.get(ot)
                        rows.append({
                            "strike": r["strike"], "type": ot,
                            "ltp": c["ltp"] if c else None,
                            "Δ": c["delta"] if c else None,
                            "θ%": c["theta_pct"] if c else None,
                            "IV%": round(c["iv"] * 100, 1) if c else None,
                        })
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    # ---------------- signals / trades ----------------
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Recent signals")
        sigs = s.get("recent_signals", [])
        if sigs:
            df = pd.DataFrame(sigs)[["ts", "strategy", "underlying", "option_type", "strike", "direction", "reason"]]
            df["ts"] = df["ts"].apply(lambda t: time.strftime("%H:%M", time.localtime(t)))
            st.dataframe(df, width="stretch", hide_index=True)
        else:
            st.caption("no signals yet")
    with col_b:
        st.subheader("Closed trades")
        trades = _store.all_trades(50)
        if trades:
            df = pd.DataFrame(trades)[["ts", "symbol", "strategy", "entry_price", "exit_price", "quantity", "pnl", "exit_reason"]]
            df["ts"] = df["ts"].apply(lambda t: time.strftime("%d %b %H:%M", time.localtime(t)))
            st.dataframe(df, width="stretch", hide_index=True)
        else:
            st.caption("no closed trades yet")


main()

# auto-refresh loop
while True:
    time.sleep(5)
    st.rerun()