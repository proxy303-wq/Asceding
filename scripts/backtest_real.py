"""Real-data backtest: replay actual NIFTY/BANKNIFTY 1m index history from DHAN
through the full engine (strategies -> risk -> paper execution -> exits) and report
per-strategy stats plus P&L by entry hour.

Option premiums are modeled with Black-Scholes on the real index path (IV from
realized vol) - fills are approximations, not broker prints. Requires DHAN
credentials + an active data plan.

Run:  python scripts/backtest_real.py --days 20 [--underlying NIFTY,BANKNIFTY]
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analytics import greeks as g  # noqa: E402
from src.analytics import indicators as ta  # noqa: E402
from src.broker.base import ChainSnapshot, OptionRow, Quote  # noqa: E402
from src.broker.dhan_live import DhanClient  # noqa: E402
from src.broker.paper import PaperBroker  # noqa: E402
from src.config import load_config  # noqa: E402
from src.db.store import Store  # noqa: E402
from src.engine.execution import ExecutionManager  # noqa: E402
from src.engine.risk import RiskManager  # noqa: E402
from src.engine.signal_engine import MarketState, SignalEngine  # noqa: E402
from src.market.candles import CandleSeries, resample  # noqa: E402
from src.strategies.breakout import BreakoutStrategy  # noqa: E402
from src.strategies.candlestick import CandlestickStrategy  # noqa: E402
from src.strategies.contrarian import ContrarianStrategy  # noqa: E402
from src.strategies.momentum import TrendMomentumStrategy  # noqa: E402

R = 0.065


def synth_chain(spot: float, ts: int, expiry: str, iv: float, interval: float, lot: int) -> ChainSnapshot:
    snap = ChainSnapshot(underlying="", expiry=expiry, spot=spot, ts=ts)
    atm = round(spot / interval) * interval
    for k in range(-10, 11):
        strike = atm + k * interval
        for ot, cp in (("CE", "C"), ("PE", "P")):
            prem = g.bs_price(cp, spot, strike, 7 / 365.0, R, iv)
            gr = g.bs_greeks(cp, spot, strike, 7 / 365.0, R, iv)
            snap.rows[(strike, ot)] = OptionRow(
                security_id=f"S{int(strike)}{ot}", symbol=f"{strike} {ot}",
                underlying="", expiry=expiry, strike=strike, option_type=ot,
                ltp=round(prem, 2), oi=80000.0, volume=5000.0, oi_change=50.0,
                iv=iv, delta=gr["delta"], gamma=gr["gamma"],
                theta=gr["theta"], vega=gr["vega"],
            )
    return snap


def rv_annual(closes: list[float]) -> float:
    if len(closes) < 20:
        return 0.15
    rets = [math.log(closes[j] / closes[j - 1]) for j in range(1, len(closes)) if closes[j - 1] > 0]
    if not rets:
        return 0.15
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) * math.sqrt(375 * 252)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--underlying", default="NIFTY,BANKNIFTY")
    ap.add_argument("--from", dest="from_date", default="")
    ap.add_argument("--to", dest="to_date", default="")
    ap.add_argument("--tf", type=int, default=1, choices=[1, 5],
                    help="candle timeframe: 1 = live-matching (default), 5 = robustness check")
    args = ap.parse_args()

    cfg = load_config()
    cfg["mode"] = "paper"
    if not cfg.get("dhan_access_token"):
        print("No DHAN access token. Set DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN (or TOTP) first.")
        sys.exit(1)
    data = DhanClient(cfg["dhan_client_id"], cfg["dhan_access_token"])

    underlyings = [u for u in cfg["instruments"] if u["underlying"] in args.underlying.split(",")]
    if not underlyings:
        print("no matching underlyings in config.yaml")
        sys.exit(1)

    store = Store("data/backtest_real.db")
    bar_clock = {"dt": datetime.now()}
    risk = RiskManager(cfg, store, lot_sizes={u["underlying"]: u.get("lot_size", 65)
                                              for u in underlyings},
                       clock=lambda: bar_clock["dt"])
    broker = PaperBroker(cfg["capital"]["initial"])
    exec_mgr = ExecutionManager(broker, risk, store, "paper")
    strategies = [
        TrendMomentumStrategy(cfg, cfg["strategies"]["momentum"]),
        BreakoutStrategy(cfg, cfg["strategies"]["breakout"]),
        CandlestickStrategy(cfg, cfg["strategies"]["candlestick"]),
        ContrarianStrategy(cfg, cfg["strategies"]["contrarian"]),
    ]
    engine = SignalEngine(strategies, risk, cfg["instruments"], cfg)

    quotes = {}
    exec_mgr.set_quote_provider(lambda sid: quotes.get(sid, Quote(security_id=sid, symbol=sid, ltp=0.0)))

    to_d = datetime.strptime(args.to_date, "%Y-%m-%d").date() if args.to_date else datetime.now().date()
    from_d = (to_d - timedelta(days=args.days)) if not args.from_date else datetime.strptime(args.from_date, "%Y-%m-%d").date()

    print(f"Fetching {from_d} .. {to_d} 1m history for {[u['underlying'] for u in underlyings]} ...")
    by_underlying = {}
    for u in underlyings:
        name = u["underlying"]
        rows_all = []
        d = from_d
        while d <= to_d:
            if d.weekday() >= 5:
                d += timedelta(days=1)
                continue
            try:
                rows = data.intraday_minute(u["security_id"], "IDX_I", "INDEX",
                                            d.isoformat(), (d + timedelta(days=1)).isoformat(), args.tf)
                rows_all.extend(rows)
            except Exception as e:
                print(f"  {name} {d}: fetch failed ({e}) - data plan / token issue?")
            d += timedelta(days=1)
        by_underlying[name] = rows_all
        print(f"  {name}: {len(rows_all)} rows")

    if not any(by_underlying.values()):
        print("No historical data fetched. Check the DHAN data plan / token validity.")
        sys.exit(1)

    series: dict[str, CandleSeries] = {}
    for name in by_underlying:
        series[name] = CandleSeries(interval_sec=args.tf * 60)
        series[name].seed(by_underlying[name])

    # replay in minute order across all underlyings
    all_bars = []
    for name, rows in by_underlying.items():
        for r in rows:
            all_bars.append((name, int(r["timestamp"]), r))
    all_bars.sort(key=lambda x: x[1])

    prev_equity = None
    hour_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    strat_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
    iv_hist: dict[str, list] = {}
    day = None

    for name, ts, bar in all_bars:
        s = series[name]
        completed = s.append_tick(ts, bar["close"], bar.get("volume", 0))
        cur_day = datetime.fromtimestamp(ts).date()
        if day is not None and cur_day != day:
            exec_mgr.exit_non_btst("EOD")
            exec_mgr.exit_btst("BTST_OPEN_EXIT")
            eq = broker.cash + sum(p.unrealized for p in broker.get_positions())
            if prev_equity is not None:
                pass
            prev_equity = eq
        day = cur_day
        if cur_day == day and (completed is not None):
            pass
        bar_clock["dt"] = datetime.fromtimestamp(ts)

        closes = s.closes()
        if len(closes) < 40:
            continue
        spot = closes[-1]
        iv = max(0.05, rv_annual(closes[-60:]) * 1.06)
        iv_hist.setdefault(name, []).append(iv)
        if len(iv_hist[name]) > 60:
            iv_hist[name].pop(0)
        iv_pct = 100.0 * sum(1 for v in iv_hist[name] if v <= iv) / len(iv_hist[name])
        ucfg = next(u for u in underlyings if u["underlying"] == name)
        interval = float(ucfg.get("strike_interval", 50))
        lot = int(ucfg.get("lot_size", 65))
        exp = (datetime.fromtimestamp(ts).date() + timedelta(days=5)).isoformat()
        chain = synth_chain(spot, ts, exp, iv, interval, lot)
        chain.underlying = name
        for (strike, ot), row in chain.rows.items():
            quotes[row.security_id] = Quote(security_id=row.security_id, symbol=row.symbol,
                                            ltp=row.ltp, bid=row.bid, ask=row.ask, ts=ts)

        highs, lows = s.highs(), s.lows()
        vols = [c.volume for c in s.candles]
        ind = {}
        ind["ema_fast_1m"] = float(ta.ema(closes, 9)[-1])
        ind["ema_slow_1m"] = float(ta.ema(closes, 21)[-1])
        ind["rsi_1m"] = float(ta.rsi(closes, 14)[-1])
        a = ta.atr(highs, lows, closes, 14)
        ind["atr_1m"] = float(a[-1])
        ind["atr_ma_1m"] = float(sum(v for v in a[-20:] if v == v) / 20)
        ind["vwap_1m"] = float(ta.vwap(closes, vols)[-1])
        ind["vol_avg_1m"] = float(sum(vols[-20:]) / 20)
        s5 = CandleSeries(interval_sec=300)
        s5.seed([{"timestamp": c.ts, "open": c.open, "high": c.high, "low": c.low,
                  "close": c.close, "volume": c.volume} for c in resample(s.candles, 5)])
        c5, h5, l5 = s5.closes(), s5.highs(), s5.lows()
        if len(c5) >= 25:
            ind["ema_fast_5m"] = float(ta.ema(c5, 9)[-1])
            ind["ema_slow_5m"] = float(ta.ema(c5, 21)[-1])
            ind["atr_5m"] = float(ta.atr(h5, l5, c5, 14)[-1])
            ind["adx_5m"] = float(ta.adx(h5, l5, c5, 14)[-1])
        ind["or_high"], ind["or_low"] = 0.0, 0.0

        st = MarketState(underlying=name, spot=spot, ts=ts, chain=chain,
                         series_1m=s, series_5m=s5, iv_percentile=iv_pct,
                         indicators=ind,
                         underlying_cfg={"name": name, "strike_interval": interval, "lot_size": lot})
        hm = datetime.fromtimestamp(ts).hour * 100 + datetime.fromtimestamp(ts).minute
        open_pos = len(exec_mgr.open)
        open_exposure = sum((quotes.get(t.security_id, Quote("", "", 0)).ltp or t.entry_price) * t.qty
                            for t in exec_mgr.open.values())
        equity = broker.cash + sum(p.unrealized for p in broker.get_positions())
        intents = engine.run([st], open_pos, open_exposure, equity, now_hm=hm)
        for intent in intents:
            exec_mgr.enter(intent)
        exec_mgr.monitor(indicators={name: ind})
        if hm >= risk.time_exit:
            exec_mgr.exit_non_btst("TIME_EXIT")

    exec_mgr.exit_non_btst("EOD")
    exec_mgr.exit_btst("BTST_OPEN_EXIT")

    all_trades = store.all_trades(10000)
    print("\n=========== REAL-DATA BACKTEST SUMMARY ===========")
    print(f"timeframe: {args.tf}m  |  window: {from_d} .. {to_d}  |  "
          f"underlyings: {[u['underlying'] for u in underlyings]}")
    print(f"trades: {len(all_trades)}  wins: {sum(1 for t in all_trades if t['pnl'] > 0)}  "
          f"losses: {sum(1 for t in all_trades if t['pnl'] <= 0)}")
    if all_trades:
        print(f"win rate: {100.0 * sum(1 for t in all_trades if t['pnl'] > 0) / len(all_trades):.1f}%")
        print(f"gross P&L: INR {sum(t['pnl'] for t in all_trades):,.0f}")
    print("\n--- per strategy ---")
    for t in all_trades:
        strat_stats[t["strategy"]]["n"] += 1
        strat_stats[t["strategy"]]["wins"] += 1 if t["pnl"] > 0 else 0
        strat_stats[t["strategy"]]["pnl"] += t["pnl"]
        entry_ts = t["ts"]
        hour = datetime.fromtimestamp(entry_ts).hour
        hour_stats[hour]["n"] += 1
        hour_stats[hour]["wins"] += 1 if t["pnl"] > 0 else 0
        hour_stats[hour]["pnl"] += t["pnl"]
    for name, st in sorted(strat_stats.items()):
        if st["n"]:
            print(f"  {name:12s} n={st['n']:3d} wins={st['wins']:3d} "
                  f"wr={100.0 * st['wins'] / st['n']:5.1f}% pnl=INR {st['pnl']:,.0f}")
    print("\n--- P&L by entry hour (IST) ---")
    for h in sorted(hour_stats):
        st = hour_stats[h]
        print(f"  {h:02d}:00  n={st['n']:3d} wins={st['wins']:3d} "
              f"wr={100.0 * st['wins'] / st['n'] if st['n'] else 0:5.1f}%  pnl=INR {st['pnl']:,.0f}")
    print("=" * 45)
    print("NOTE: option premiums are modeled (BS on realized vol), not broker prints.")


if __name__ == "__main__":
    main()
