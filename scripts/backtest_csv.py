"""CSV replay backtester: run the full engine on real historical NIFTY/BANKNIFTY
candles (PrOxy's CSVs: NIFTY_5m.csv, NIFTY_1m.csv, BANKNIFTY_5m.csv).

Settles the head-to-head on identical data. Volume in these CSVs is 0, so bars
are given constant volume (volume filters become pass-through, documented).
Option premiums are modeled with Black-Scholes on realized vol.

Run:
  python scripts/backtest_csv.py --tf 5 --days 240
  python scripts/backtest_csv.py --tf 5 --sl-pct 0.005 --rr 2.0 --lock   # PrOxy-style scalping
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analytics import greeks as g  # noqa: E402
from src.analytics import indicators as ta  # noqa: E402
from src.broker.base import ChainSnapshot, OptionRow, Quote  # noqa: E402
from src.broker.paper import PaperBroker  # noqa: E402
from src.config import load_config  # noqa: E402
from src.db.store import Store  # noqa: E402
from src.engine.execution import ExecutionManager  # noqa: E402
from src.engine.risk import RiskManager  # noqa: E402
from src.engine.signal_engine import MarketState, SignalEngine  # noqa: E402
from src.market.candles import CandleSeries, resample  # noqa: E402
from src.strategies.breakout import BreakoutStrategy  # noqa: E402
from src.strategies.candlestick import CandlestickStrategy  # noqa: E402
from src.strategies.levels import LevelPrimaryStrategy  # noqa: E402
from src.strategies.meanrev import MeanReversionStrategy  # noqa: E402
from src.strategies.contrarian import ContrarianStrategy  # noqa: E402
from src.strategies.momentum import TrendMomentumStrategy  # noqa: E402

R = 0.065
DATA = Path(r"C:\PrOxyTradingTerminal\data")


def load_csv(path: Path) -> list[dict]:
    import csv
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ts = int(datetime.fromisoformat(r["date"]).timestamp())
            except Exception:
                continue
            rows.append({
                "timestamp": ts,
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
                "volume": 1000.0,   # CSV volume is 0 -> constant, filters pass-through
            })
    return sorted(rows, key=lambda r: r["timestamp"])


def synth_chain(spot, ts, expiry, iv, interval, lot):
    snap = ChainSnapshot(underlying="", expiry=expiry, spot=spot, ts=ts)
    atm = round(spot / interval) * interval
    for k in range(-10, 11):
        strike = atm + k * interval
        for ot, cp in (("CE", "C"), ("PE", "P")):
            prem = g.bs_price(cp, spot, strike, 7 / 365.0, R, iv)
            gr = g.bs_greeks(cp, spot, strike, 7 / 365.0, R, iv)
            snap.rows[(strike, ot)] = OptionRow(
                security_id=f"S{int(strike)}{ot}", symbol=f"{strike} {ot}", underlying="",
                expiry=expiry, strike=strike, option_type=ot, ltp=round(prem, 2),
                bid=round(prem * 0.992, 2), ask=round(prem * 1.008, 2),   # 0.8% spread (realistic)
                oi=80000.0, volume=5000.0, oi_change=50.0, iv=iv,
                delta=gr["delta"], gamma=gr["gamma"], theta=gr["theta"], vega=gr["vega"])
    return snap


def rv_annual(closes):
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
    ap.add_argument("--tf", type=int, default=5, choices=[1, 5])
    ap.add_argument("--days", type=int, default=240)
    ap.add_argument("--underlying", default="NIFTY,BANKNIFTY")
    ap.add_argument("--sl-pct", type=float, default=None)
    ap.add_argument("--rr", type=float, default=None)
    ap.add_argument("--exit", default=None)
    ap.add_argument("--lock", action="store_true", help="enable premium-% lock-profit trail")
    ap.add_argument("--conviction", type=float, default=None)
    ap.add_argument("--confirm", type=int, default=None, help="override signal confirmation bars")
    ap.add_argument("--wick", type=float, default=None, help="levels: wick_atr tolerance")
    ap.add_argument("--break", dest="break_atr", type=float, default=None, help="levels: break_atr")
    ap.add_argument("--look", type=float, default=None, help="levels: look_atr")
    ap.add_argument("--min-confirm", type=float, default=None, help="levels: min_confirm score")
    ap.add_argument("--no-btst-stocks", action="store_true")
    ap.add_argument("--db", default="data/backtest_csv.db")
    args = ap.parse_args()

    cfg = load_config()
    cfg["mode"] = "paper"
    if args.sl_pct is not None:
        cfg["risk"]["sl_pct"] = args.sl_pct
    if args.rr is not None:
        cfg["risk"]["reward_risk"] = args.rr
    if args.exit:
        cfg["risk"]["exit_mode"] = args.exit
    if args.lock:
        cfg["risk"]["lock_profit"]["enabled"] = True
    if args.conviction is not None:
        cfg["signal_quality"]["min_conviction"] = args.conviction
    if args.confirm is not None:
        cfg["signal_quality"]["confirm_bars"] = args.confirm
    lv = cfg["strategies"]["levels"]
    if args.wick is not None: lv["wick_atr"] = args.wick
    if args.break_atr is not None: lv["break_atr"] = args.break_atr
    if args.look is not None: lv["look_atr"] = args.look
    if args.min_confirm is not None: lv["min_confirm"] = args.min_confirm
    cfg["stock_btst"]["enabled"] = not args.no_btst_stocks
    for s in cfg.get("strategies", {}).values():
        s["enabled"] = s.get("enabled", True)

    files = {
        ("NIFTY", 1): DATA / "NIFTY_1m.csv",
        ("NIFTY", 5): DATA / "NIFTY_5m.csv",
        ("BANKNIFTY", 5): DATA / "BANKNIFTY_5m.csv",
        ("BANKNIFTY", 1): DATA / "NIFTY_1m.csv",   # placeholder; only 5m exists
    }
    class BacktestStore(Store):
        def __init__(self, path):
            super().__init__(path)
            self.clock_ts = None

        def record_trade(self, *a, **kw):
            if self.clock_ts is not None:
                kw["ts"] = self.clock_ts
            return super().record_trade(*a, **kw)

        def record_ml_sample(self, *a, **kw):
            if self.clock_ts is not None:
                kw["ts"] = self.clock_ts
            return super().record_ml_sample(*a, **kw)

    store = BacktestStore(args.db)
    bar_clock = {"dt": datetime.now()}
    risk = RiskManager(cfg, store, lot_sizes={"NIFTY": 65, "BANKNIFTY": 30},
                       clock=lambda: bar_clock["dt"])
    broker = PaperBroker(cfg["capital"]["initial"], slippage_bps=6.0)
    exec_mgr = ExecutionManager(broker, risk, store, "paper")
    strategies = [
        TrendMomentumStrategy(cfg, cfg["strategies"]["momentum"]),
        BreakoutStrategy(cfg, cfg["strategies"]["breakout"]),
        CandlestickStrategy(cfg, cfg["strategies"]["candlestick"]),
        LevelPrimaryStrategy(cfg, cfg["strategies"]["levels"]),
        MeanReversionStrategy(cfg, cfg["strategies"]["meanrev"]),
        ContrarianStrategy(cfg, cfg["strategies"]["contrarian"]),
    ]
    engine = SignalEngine(strategies, risk, cfg["instruments"], cfg)
    quotes = {}
    exec_mgr.set_quote_provider(lambda sid: quotes.get(sid, Quote(security_id=sid, symbol=sid, ltp=0.0)))

    underlyings = [u.strip() for u in args.underlying.split(",") if u.strip()]
    series: dict[str, CandleSeries] = {}
    all_bars: list[tuple[str, int, dict]] = []
    for name in underlyings:
        f = files.get((name, args.tf))
        if f is None or not f.exists():
            print(f"no {args.tf}m data for {name}")
            continue
        rows = load_csv(f)
        if args.tf == 1 and name == "BANKNIFTY":
            print("BANKNIFTY has no 1m CSV - using NIFTY 1m placeholder is wrong; skipped")
            continue
        # limit to last N calendar days
        if args.days and args.days < 10000:
            cutoff = rows[-1]["timestamp"] - args.days * 86400
            rows = [r for r in rows if r["timestamp"] >= cutoff]
        # seed only a warmup, then REPLAY one bar at a time (no look-ahead bias)
        series[name] = CandleSeries(interval_sec=args.tf * 60)
        warmup = rows[:80]
        series[name].seed(warmup)
        for r in rows[80:]:
            all_bars.append((name, r["timestamp"], r))
        print(f"{name} {args.tf}m: {len(rows)} bars ({datetime.fromtimestamp(rows[0]['timestamp']).date()} .. "
              f"{datetime.fromtimestamp(rows[-1]['timestamp']).date()}), warmup {len(warmup)}")
    all_bars.sort(key=lambda x: (x[1], x[0]))

    iv_hist: dict[str, list] = {}
    day = None
    hour_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    strat_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})

    for name, ts, bar in all_bars:
        store.clock_ts = ts
        s = series[name]
        s.append_tick(ts, bar["close"], bar["volume"])
        cur_day = datetime.fromtimestamp(ts).date()
        if day is not None and cur_day != day:
            exec_mgr.exit_non_btst("EOD")
        day = cur_day
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
        interval = 50 if name == "NIFTY" else 100
        lot = 65 if name == "NIFTY" else 30
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
        s5 = CandleSeries(interval_sec=args.tf * 60)
        s5.seed([{"timestamp": c.ts, "open": c.open, "high": c.high, "low": c.low,
                  "close": c.close, "volume": c.volume} for c in resample(s.candles, args.tf)])
        c5, h5, l5 = s5.closes(), s5.highs(), s5.lows()
        if len(c5) >= 25:
            ind["ema_fast_5m"] = float(ta.ema(c5, 9)[-1])
            es5 = ta.ema(c5, 21)
            ind["ema_slow_5m"] = float(es5[-1])
            ind["ema_slow_prev_5m"] = float(es5[-2]) if len(es5) > 1 and es5[-2] == es5[-2] else None
            ind["atr_5m"] = float(ta.atr(h5, l5, c5, 14)[-1])
            ind["adx_5m"] = float(ta.adx(h5, l5, c5, 14)[-1])
        # opening range: first 15 min of the day
        d0 = datetime.fromtimestamp(ts).date()
        or_c = [c for c in s.candles if datetime.fromtimestamp(c.ts).date() == d0
                and datetime.fromtimestamp(c.ts).minute < 30]
        if or_c:
            ind["or_high"] = max(c.high for c in or_c)
            ind["or_low"] = min(c.low for c in or_c)
        else:
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
        if hm >= 1500:
            exec_mgr.exit_non_btst("EOD_LATE")

    exec_mgr.exit_non_btst("EOD")
    exec_mgr.exit_btst("BTST_OPEN_EXIT")

    all_trades = store.all_trades(100000)
    print("\n================ CSV BACKTEST ================")
    print(f"trades: {len(all_trades)}  wins: {sum(1 for t in all_trades if t['pnl'] > 0)}  "
          f"losses: {sum(1 for t in all_trades if t['pnl'] <= 0)}")
    if all_trades:
        print(f"win rate: {100.0 * sum(1 for t in all_trades if t['pnl'] > 0) / len(all_trades):.1f}%")
        print(f"gross P&L: INR {sum(t['pnl'] for t in all_trades):,.0f}")
        print(f"avg win: INR {sum(t['pnl'] for t in all_trades if t['pnl'] > 0) / max(1, sum(1 for t in all_trades if t['pnl'] > 0)):,.0f}  "
              f"avg loss: INR {sum(t['pnl'] for t in all_trades if t['pnl'] <= 0) / max(1, sum(1 for t in all_trades if t['pnl'] <= 0)):,.0f}")
        pf = sum(t['pnl'] for t in all_trades if t['pnl'] > 0) / max(1, abs(sum(t['pnl'] for t in all_trades if t['pnl'] < 0)))
        print(f"profit factor: {pf:.2f}")
        from collections import Counter
        print(f"exits: {dict(Counter(t['exit_reason'] for t in all_trades))}")
    print("\n--- per strategy ---")
    for t in all_trades:
        strat_stats[t["strategy"]]["n"] += 1
        strat_stats[t["strategy"]]["wins"] += 1 if t["pnl"] > 0 else 0
        strat_stats[t["strategy"]]["pnl"] += t["pnl"]
    for name2, st2 in sorted(strat_stats.items()):
        if st2["n"]:
            print(f"  {name2:12s} n={st2['n']:4d} wins={st2['wins']:4d} "
                  f"wr={100.0 * st2['wins'] / st2['n']:5.1f}% pnl=INR {st2['pnl']:,.0f}")
    print("\n--- P&L by entry hour (IST) ---")
    import zoneinfo
    _ist = zoneinfo.ZoneInfo("Asia/Kolkata")
    for t in all_trades:
        h = datetime.fromtimestamp(t["ts"], tz=_ist).hour
        hour_stats[h]["n"] += 1
        hour_stats[h]["wins"] += 1 if t["pnl"] > 0 else 0
        hour_stats[h]["pnl"] += t["pnl"]
    for h in sorted(hour_stats):
        st3 = hour_stats[h]
        if st3["n"]:
            print(f"  {h:02d}:00  n={st3['n']:3d} wins={st3['wins']:3d} "
                  f"wr={100.0 * st3['wins'] / st3['n']:5.1f}%  pnl=INR {st3['pnl']:,.0f}")
    eq_curve = store.equity_curve(100000)
    if len(eq_curve) > 5:
        vals = [e["equity"] for e in eq_curve]
        rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1] > 0]
        import statistics
        mean_r = sum(rets) / len(rets)
        sd = statistics.pstdev(rets) or 1e-9
        downside = [r for r in rets if r < 0]
        dsd = statistics.pstdev(downside) if downside else 1e-9
        sharpe = mean_r / sd * math.sqrt(252)
        sortino = mean_r / dsd * math.sqrt(252)
        peak = 0.0
        max_dd = 0.0
        for v in vals:
            peak = max(peak, v)
            max_dd = max(max_dd, (peak - v) / peak * 100.0)
        print(f"\nannualized Sharpe: {sharpe:.2f}  Sortino: {sortino:.2f}  max drawdown: {max_dd:.2f}%")
    print("=" * 42)
    print("NOTE: premiums modeled (BS on realized vol); CSV volume is 0 (filters pass-through).")
    print("ML samples in data/backtest_csv.db -> run scripts/train_ml.py after pointing db_path there.")


if __name__ == "__main__":
    main()
