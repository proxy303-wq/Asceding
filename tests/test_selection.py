"""Tests for theta-aware strike selection."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.analytics import greeks as g  # noqa: E402
from src.broker.base import ChainSnapshot, OptionRow  # noqa: E402
from src.market.instruments import select_expiry  # noqa: E402
from src.market.selection import chain_ladder, select_best_strike, theta_pct_per_day  # noqa: E402

SPOT = 23500.0
IV = 0.13
T = 7 / 365.0
R = 0.065


def build_chain(spot=SPOT, iv=IV):
    snap = ChainSnapshot(underlying="NIFTY", expiry="2026-08-21", spot=spot,
                         ts=int(time.time()))
    atm = round(spot / 50) * 50
    for k in range(-6, 7):
        strike = atm + k * 50
        for ot, cp in (("CE", "C"), ("PE", "P")):
            premium = g.bs_price(cp, spot, strike, T, R, iv)
            gr = g.bs_greeks(cp, spot, strike, T, R, iv)
            snap.rows[(strike, ot)] = OptionRow(
                security_id=f"S{strike}{ot}", symbol=f"NIFTY {strike} {ot}",
                underlying="NIFTY", expiry="2026-08-21", strike=strike, option_type=ot,
                ltp=round(premium, 2), oi=80000.0, volume=5000.0,
                iv=iv, delta=gr["delta"], theta=gr["theta"],
            )
    return snap


def test_theta_pct_atm_vs_itm():
    snap = build_chain()
    atm = snap.atm_strike(50)
    atm_t = theta_pct_per_day(snap.get(atm, "CE"))
    itm_t = theta_pct_per_day(snap.get(atm - 100, "CE"))
    assert atm_t > 0
    assert itm_t < atm_t, (itm_t, atm_t)   # ITM bleeds less relative to premium


def test_select_prefers_low_theta():
    snap = build_chain()
    best = select_best_strike(snap, "CE", 50.0, {"mode": "theta_optimized", "width": 3})
    assert best is not None
    atm = snap.atm_strike(50)
    assert theta_pct_per_day(best) <= theta_pct_per_day(snap.get(atm, "CE")) + 1e-9


def test_select_fixed_uses_hint():
    snap = build_chain()
    row = select_best_strike(snap, "CE", 50.0, {"mode": "fixed"}, hint_strike=23600.0)
    assert row.strike == 23600.0


def test_ladder_shape():
    snap = build_chain()
    ladder = chain_ladder(snap, 50.0, width=2)
    assert len(ladder) == 5
    assert ladder[0]["strike"] < ladder[-1]["strike"]
    assert "CE" in ladder[0] and "PE" in ladder[0]
    assert ladder[0]["CE"]["theta_pct"] > 0


def test_expiry_nearest_policy():
    exps = ["2026-08-20", "2026-08-27", "2026-09-03"]
    from datetime import datetime
    now = datetime(2026, 8, 19, 12, 0)
    assert select_expiry(exps, "nearest", now=now) == "2026-08-20"


def test_expiry_dte_window_skips_theta_trap():
    from datetime import datetime
    now = datetime(2026, 8, 20, 12, 0)          # expiry day for 2026-08-20 (dte ~0.1)
    exps = ["2026-08-20", "2026-08-27", "2026-09-03"]
    picked = select_expiry(exps, "dte_window", min_dte=1.0,
                           prefer_min=2, prefer_max=5, now=now)
    assert picked == "2026-08-27"              # skips theta-trap nearest, prefers 2-5 days


def test_expiry_dte_window_prefers_sweet_spot():
    from datetime import datetime
    now = datetime(2026, 8, 22, 12, 0)
    exps = ["2026-08-27", "2026-09-03", "2026-09-10"]
    picked = select_expiry(exps, "dte_window", min_dte=0.1,
                           prefer_min=2, prefer_max=5, now=now)
    assert picked == "2026-08-27"              # dte ~5.1 -> closest to window


if __name__ == "__main__":
    for fn in [test_theta_pct_atm_vs_itm, test_select_prefers_low_theta,
               test_select_fixed_uses_hint, test_ladder_shape,
               test_expiry_nearest_policy, test_expiry_dte_window_skips_theta_trap,
               test_expiry_dte_window_prefers_sweet_spot]:
        fn()
        print("ok " + fn.__name__)
    print("ALL SELECTION TESTS PASSED")
