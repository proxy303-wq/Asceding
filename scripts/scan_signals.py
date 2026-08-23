"""One-shot signal scan: print what the strategies would do right now (no orders)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ist_now, market_is_open  # noqa: E402
from src.engine.loop import AutoTrader  # noqa: E402


def main():
    if not market_is_open():
        print("Market closed (NSE hours 09:15-15:30 IST). Nothing to scan.")
        return
    t = AutoTrader()
    for u in t.instruments:
        t._seed_series(u)
    t._refresh_quotes()
    t._refresh_chains()
    for u in t.instruments:
        t._update_candles(u)
    states = []
    for u in t.instruments:
        chain = t.chain_svc._cache.get(f"{u.name}:{t._current_expiry(u)}")
        states.append(t._make_state(u, chain))
    from src.strategies.base import StrategyContext
    print(f"--- scan @ {ist_now().strftime('%H:%M:%S')} IST ---")
    for st in states:
        ucfg = {"name": st.underlying, "strike_interval": st.underlying_cfg.get("strike_interval", 50),
                "lot_size": st.underlying_cfg.get("lot_size", 0)}
        for strat in t.strategies:
            if not strat.enabled():
                continue
            ctx = StrategyContext(underlying=st.underlying, spot=st.spot, ts=st.ts, chain=st.chain,
                                  iv_percentile=st.iv_percentile, series_1m=st.series_1m,
                                  series_5m=st.series_5m, daily_series=st.daily,
                                  indicators=st.indicators, config=ucfg)
            for sig in strat.evaluate(ctx):
                print(f"  [{strat.name}] {sig.side} {sig.underlying} {sig.option_type} {sig.strike} "
                      f"@{sig.entry_price_hint:.2f} | {sig.reason}")


if __name__ == "__main__":
    main()
