"""Offline backtest / pipeline smoke test with synthetic regime data.

Generates N days of 1-minute synthetic NIFTY data (trending + range days) with a
synthetic option chain priced by Black-Scholes, then runs the real strategies,
risk engine, paper broker and execution manager end to end. No credentials or
network needed. Use --days to control length.

Run:  python scripts/backtest.py --days 40
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analytics import greeks as g  # noqa: E402
from src.analytics import indicators as ta  # noqa: E402
from src.broker.base import ChainSnapshot, OptionRow, Quote  # noqa: E402
from src.broker.paper import PaperBroker  # noqa: E402
from src.config import load_config  # noqa: E402
from src.db.store import Store  # noqa: E402
from src.engine.execution import ExecutionManager  # noqa: E402
from src.engine.risk import RiskManager  # noqa: E402
from src.engine.signal_engine import MarketState, SignalEngine  # noqa: E402
from src.market.candles import Candle, CandleSeries, resample  # noqa: E402
from src.strategies.breakout import BreakoutStrategy  # noqa: E402
from src.strategies.candlestick import CandlestickStrategy  # noqa: E402
from src.strategies.contrarian import ContrarianStrategy  # noqa: E402
from src.strategies.momentum import TrendMomentumStrategy  # noqa: E402

UNDERLYING = "NIFTY"
SPOT0 = 23500.0
INTERVAL = 50
LOT = 75
R = 0.065
BASE_IV = 0.13
MIN_PER_DAY = 375          # 09:15 -> 15:30


def gen_day(day_idx: int, seed: int) -> list[dict]:
    """Synthetic 1m bars for one day. Regime: 0 trend-up, 1 trend-down, 2 range."""
    rng = random.Random(seed * 1000 + day_idx)
    regime = day_idx % 3
    drift = {0: 0.00016, 1: -0.00016, 2: 0.0}[regime]
    vol = {0: 0.0009, 1: 0.0009, 2: 0.0005}[regime]
    base = SPOT0 * (1 + 0.006 * day_idx)
    price = base
    bars = []
    start = datetime(2026, 8, 3, 9, 15) + timedelta(days=day_idx)  # a Monday-ish start
    pull_phase = 0.0        # mean-reverting pullbacks toward the trend line
    for i in range(MIN_PER_DAY):
        # intraday vol clustering: open + close more volatile
        v = vol * (1.6 if (i < 45 or i > 330) else 0.8)
        shock = rng.gauss(drift, v)
        if 30 <= i <= 120 and regime == 0:
            shock += 0.0004   # morning rally
        # every ~45-70 bars, retrace toward the recent mean (pullback structure)
        if i % 60 in range(10):
            pull_phase = 0.0016 if regime != 2 else 0.0006
        else:
            pull_phase = 0.0
        if regime == 0:
            shock -= pull_phase
        elif regime == 1:
            shock += pull_phase
        price *= math.exp(shock)
        o = price * (1 + rng.gauss(0, 0.0002))
        c = price * (1 + rng.gauss(0, 0.0002))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.0003)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.0003)))
        v = max(100, rng.gauss(2000, 800)) * (1.8 if (i < 20 or 150 < i < 160) else 1.0)
        ts = int((start + timedelta(minutes=i)).timestamp())
        bars.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
    return bars


def synth_chain(spot: float, ts: int, expiry: str, iv: float, dte: float = 7.0) -> ChainSnapshot:
    snap = ChainSnapshot(underlying=UNDERLYING, expiry=expiry, spot=spot, ts=ts)
    atm = round(spot / INTERVAL) * INTERVAL
    for k in range(-10, 11):
        strike = atm + k * INTERVAL
        for ot, cp in (("CE", "C"), ("PE", "P")):
            premium = g.bs_price(cp, spot, strike, dte / 365.0, R, iv)
            gr = g.bs_greeks(cp, spot, strike, dte / 365.0, R, iv)
            sid = f"S{int(strike)}{ot}"
            spread = max(0.5, premium * 0.004)
            snap.rows[(strike, ot)] = OptionRow(
                security_id=sid, symbol=f"{UNDERLYING} {strike} {ot}", underlying=UNDERLYING,
                expiry=expiry, strike=strike, option_type=ot, ltp=round(premium, 2),
                bid=round(max(0.05, premium - spread), 2), ask=round(premium + spread, 2),
                volume=1000.0, oi=5000.0, oi_change=50.0 if k == 0 else 10.0,
                iv=iv, delta=gr["delta"], gamma=gr["gamma"],
                theta=gr["theta"], vega=gr["vega"],
            )
    return snap


def main():
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--tf", type=int, default=1, choices=[1, 5],
                    help="candle timeframe: 1 = live-matching (default), 5 = robustness check")
    args = ap.parse_args()

    cfg = load_config()
    cfg["mode"] = "paper"

    class BacktestStore(Store):
        """Stamps each trade with the current simulated bar time so daily limits
        work per simulated day instead of wall-clock day."""

        def __init__(self, path):
            super().__init__(path)
            self.clock_ts = None

        def record_trade(self, *a, **kw):
            if self.clock_ts is not None:
                kw["ts"] = self.clock_ts
            return super().record_trade(*a, **kw)

    store = BacktestStore("data/backtest.db")
    bar_clock = {"dt": datetime(2026, 8, 3, 9, 15)}

    def clock():
        return bar_clock["dt"]

    risk = RiskManager(cfg, store, lot_sizes={UNDERLYING: LOT}, clock=clock)
    broker = PaperBroker(cfg["capital"]["initial"])
    exec_mgr = ExecutionManager(broker, risk, store, "paper")
    strategies = [
        TrendMomentumStrategy(cfg, cfg["strategies"]["momentum"]),
        BreakoutStrategy(cfg, cfg["strategies"]["breakout"]),
        CandlestickStrategy(cfg, cfg["strategies"]["candlestick"]),
        ContrarianStrategy(cfg, cfg["strategies"]["contrarian"]),
    ]
    engine = SignalEngine(strategies, risk, cfg["instruments"], cfg)
    counter = {"signals": 0, "intents": 0, "entered": 0, "rejected": 0}

    def rolling_expiry(ts: int) -> str:
        # +5 days keeps fractional dte inside [min_dte, max_dte] for every bar
        return (datetime.fromtimestamp(ts).date() + timedelta(days=5)).isoformat()
    quotes: dict[str, Quote] = {}
    last_iv_pct = 50.0
    iv_history = []

    def quote_provider(sid: str) -> Quote:
        return quotes.get(sid, Quote(security_id=sid, symbol=sid, ltp=0.0))

    exec_mgr.set_quote_provider(quote_provider)

    day_idx = 0
    all_bars = []
    # warmup day
    all_bars.append(("warmup", gen_day(0, args.seed)))
    day_idx = 1

    for d in range(1, args.days + 1):
        all_bars.append((f"day{d}", gen_day(d, args.seed)))

    total_pnl = 0.0
    stats = {"trades": 0, "wins": 0, "gross_pnl": 0.0, "days_profitable": 0,
             "max_dd": 0.0, "peak_equity": cfg["capital"]["initial"]}
    series = CandleSeries(interval_sec=args.tf * 60)
    prev_day_equity = None

    def _fit_tf(bars):
        if args.tf == 1:
            return bars
        from src.market.candles import Candle, resample
        cs = CandleSeries(interval_sec=60)
        cs.seed(bars)
        r5 = resample(cs.candles, 5)
        return [{"timestamp": c.ts, "open": c.open, "high": c.high, "low": c.low,
                 "close": c.close, "volume": c.volume} for c in r5]

    for label, bars in all_bars:
        bars = _fit_tf(bars)
        for i, bar in enumerate(bars):
            ts = int(bar["timestamp"])
            store.clock_ts = ts
            bar_clock["dt"] = datetime.fromtimestamp(ts)
            series.append_tick(ts, bar["close"], bar["volume"])
            if len(series.candles) < 40:
                continue
            closes = series.closes()
            highs = series.highs()
            lows = series.lows()
            vols = [c.volume for c in series.candles]
            n = len(closes)
            if n < 40 or label == "warmup":
                continue

            spot = closes[-1]
            # IV tracks realized vol of the generated path (plus a small premium), so the
            # IV-percentile filter behaves realistically instead of oscillating wildly
            _rets = [math.log(closes[j] / closes[j - 1]) for j in range(max(1, len(closes) - 30), len(closes))
                     if closes[j - 1] > 0]
            _rv = (math.sqrt(sum((r - sum(_rets) / len(_rets)) ** 2 for r in _rets) / len(_rets))
                   * math.sqrt(375 * 252)) if len(_rets) > 5 else 0.15
            iv = max(0.05, _rv * 1.06)
            iv_history.append(iv)
            if len(iv_history) > 60:
                iv_history.pop(0)
            last_iv_pct = 100.0 * sum(1 for v in iv_history if v <= iv) / len(iv_history)

            chain = synth_chain(spot, ts, rolling_expiry(ts), iv)
            for strike, ot in chain.rows:
                row = chain.rows[(strike, ot)]
                quotes[row.security_id] = Quote(security_id=row.security_id, symbol=row.symbol,
                                                ltp=row.ltp, bid=row.bid, ask=row.ask, ts=ts)
            quotes[str(int(spot))] = Quote(security_id=str(int(spot)), symbol=UNDERLYING, ltp=spot, ts=ts)

            # indicators
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
                      "close": c.close, "volume": c.volume} for c in resample(series.candles, 5)])
            c5, h5, l5 = s5.closes(), s5.highs(), s5.lows()
            if len(c5) >= 25:
                ind["ema_fast_5m"] = float(ta.ema(c5, 9)[-1])
                ind["ema_slow_5m"] = float(ta.ema(c5, 21)[-1])
                ind["atr_5m"] = float(ta.atr(h5, l5, c5, 14)[-1])
                ind["adx_5m"] = float(ta.adx(h5, l5, c5, 14)[-1])
            # opening range: first 15 mins of the day
            day = datetime.fromtimestamp(ts).date()
            or_candles = [c for c in series.candles
                          if datetime.fromtimestamp(c.ts).date() == day and
                          datetime.fromtimestamp(c.ts).minute < 30]
            if or_candles:
                ind["or_high"] = max(c.high for c in or_candles)
                ind["or_low"] = min(c.low for c in or_candles)
            else:
                ind["or_high"], ind["or_low"] = 0.0, 0.0

            st = MarketState(underlying=UNDERLYING, spot=spot, ts=ts, chain=chain,
                             series_1m=series, series_5m=s5, iv_percentile=last_iv_pct,
                             indicators=ind,
                             underlying_cfg={"name": UNDERLYING, "strike_interval": INTERVAL,
                                             "lot_size": LOT})
            now_hm = datetime.fromtimestamp(ts).hour * 100 + datetime.fromtimestamp(ts).minute
            open_pos = len(exec_mgr.open)
            open_exposure = sum((quotes.get(t.security_id, Quote("", "", 0)).ltp or t.entry_price) * t.qty
                                for t in exec_mgr.open.values())
            equity = broker.cash + sum(p.unrealized for p in broker.get_positions())
            intents = engine.run([st], open_pos, open_exposure, equity, now_hm=now_hm)
            counter["intents"] += len(intents)
            for intent in intents:
                ok, _ = exec_mgr.enter(intent)
                counter["entered"] += 1 if ok else 0
            exec_mgr.monitor(indicators={UNDERLYING: ind})
            if i == 0:
                exec_mgr.exit_btst("BTST_OPEN_EXIT")     # close overnight holds at the open
            if now_hm >= risk.time_exit:
                exec_mgr.exit_non_btst("TIME_EXIT")

        # end of day: flatten intraday legs (BTST positions stay overnight)
        exec_mgr.exit_non_btst("EOD")
        equity_now = broker.cash + sum(p.unrealized for p in broker.get_positions())
        if prev_day_equity is not None:
            day_pnl = equity_now - prev_day_equity
            total_pnl += day_pnl
            stats["days_profitable"] += 1 if day_pnl > 0 else 0
        prev_day_equity = equity_now
        stats["peak_equity"] = max(stats["peak_equity"], equity_now)
        stats["max_dd"] = max(stats["max_dd"], (stats["peak_equity"] - equity_now) / cfg["capital"]["initial"] * 100.0)

    # rough signal count: strategies evaluated directly per bar would be heavy; use engine signals
    counter["signals"] = counter["intents"]
    all = store.all_trades(10000)
    stats["trades"] = len(all)
    stats["wins"] = sum(1 for t in all if t["pnl"] > 0)
    stats["gross_pnl"] = sum(t["pnl"] for t in all)
    from collections import Counter
    reasons = Counter(t["exit_reason"] for t in all)

    print("\n================ BACKTEST SUMMARY ================")
    print(f"signals / intents  : {counter['signals']} / {counter['intents']} (entered {counter['entered']})")
    print(f"timeframe           : {args.tf}m")
    print(f"days simulated      : {args.days}")
    print(f"trades             : {stats['trades']}")
    print(f"wins / losses      : {stats['wins']} / {stats['trades'] - stats['wins']}")
    print(f"win rate           : {100.0 * stats['wins'] / stats['trades']:.1f}%" if stats['trades'] else "n/a")
    print(f"gross P&L          : INR {stats['gross_pnl']:,.0f}")
    print(f"days profitable    : {stats['days_profitable']} / {args.days}")
    print(f"exit reasons        : {dict(reasons)}")
    from collections import defaultdict
    hour_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    strat_stats = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in all:
        strat_stats[t["strategy"]]["n"] += 1
        strat_stats[t["strategy"]]["wins"] += 1 if t["pnl"] > 0 else 0
        strat_stats[t["strategy"]]["pnl"] += t["pnl"]
        hour = datetime.fromtimestamp(t["ts"]).hour
        hour_stats[hour]["n"] += 1
        hour_stats[hour]["wins"] += 1 if t["pnl"] > 0 else 0
        hour_stats[hour]["pnl"] += t["pnl"]
    print("--- per strategy ---")
    for name, st in sorted(strat_stats.items()):
        if st["n"]:
            print(f"  {name:12s} n={st['n']:3d} wins={st['wins']:3d} "
                  f"wr={100.0 * st['wins'] / st['n']:5.1f}% pnl=INR {st['pnl']:,.0f}")
    print("--- P&L by entry hour ---")
    for h in sorted(hour_stats):
        st = hour_stats[h]
        if st["n"]:
            print(f"  {h:02d}:00  n={st['n']:3d} wins={st['wins']:3d} "
                  f"wr={100.0 * st['wins'] / st['n']:5.1f}%  pnl=INR {st['pnl']:,.0f}")
    print(f"max drawdown       : {stats['max_dd']:.2f}%")
    print(f"final equity       : INR {broker.cash:,.0f} (start INR {cfg['capital']['initial']:,.0f})")
    print("===================================================")
    print("NOTE: synthetic data. Real results will differ. Tune in paper mode first.")


if __name__ == "__main__":
    main()
