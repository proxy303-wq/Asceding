"""Parameter sweep for the LEVEL-PRIMARY strategy on real CSV data.

Runs the CSV backtester with a grid of tolerance/risk combos, ranks by profit
factor (min trades), then optionally validates the top configs on a longer
out-of-sample window.

Run:
  python scripts/sweep_levels.py --days 30                # sweep on 1 month
  python scripts/sweep_levels.py --days 30 --validate 90 # validate top-3 on 3 months
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GRID = {
    "wick_atr":   [0.10, 0.20, 0.30],
    "min_confirm": [35, 45],
    "sl_pct":     [0.20, 0.30],
    "confirm":    [1],
}


def run_combo(args, params: dict) -> dict:
    cmd = ["python", "scripts/backtest_csv.py", "--tf", "5",
           "--days", str(args.days), "--underlying", args.underlying,
           "--db", f"data/sweep_{abs(hash(frozenset(params.items()))) % 10**6}.db"]
    FLAG = {"wick_atr": "--wick", "min_confirm": "--min-confirm",
            "sl_pct": "--sl-pct", "confirm": "--confirm", "break_atr": "--break"}
    for k, v in params.items():
        cmd += [FLAG.get(k, f"--{k}"), str(v)]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=args.timeout)
    out = p.stdout
    def grab(pattern, default=0.0):
        m = re.search(pattern, out)
        return float(m.group(1).replace(",", "")) if m else default
    return {
        **params,
        "trades": int(grab(r"trades: (\d+)", 0)),
        "win_rate": grab(r"win rate: ([\d.]+)%"),
        "pnl": grab(r"gross P&L: INR ([-+\d,]+)"),
        "pf": grab(r"profit factor: ([\d.]+)"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--validate", type=int, default=0, help="re-run top N on this many days")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--underlying", default="NIFTY,BANKNIFTY")
    args = ap.parse_args()

    # build the cartesian grid (cap it)
    combos = [dict(zip(GRID, c)) for c in __import__("itertools").product(*GRID.values())]
    print(f"sweeping {len(combos)} combos on {args.days} days ...")
    results = []
    for i, params in enumerate(combos, 1):
        try:
            r = run_combo(args, params)
        except subprocess.TimeoutExpired:
            r = {**params, "trades": -1, "pnl": 0, "pf": 0, "win_rate": 0}
        r["pnl"] = float(str(r["pnl"]).replace(",", ""))
        results.append(r)
        print(f"[{i}/{len(combos)}] {params} -> trades={r['trades']} wr={r['win_rate']:.1f}% "
              f"pnl={r['pnl']:,.0f} pf={r['pf']:.2f}")

    good = [r for r in results if r["trades"] >= 5]
    good.sort(key=lambda r: r["pf"], reverse=True)
    print("\n===== TOP CONFIGS (by profit factor, min 5 trades) =====")
    for r in good[:8]:
        print(f"  {r}")
    if not good:
        print("  no config reached 5 trades")

    Path("data/sweep_results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")

    if args.validate and good:
        print(f"\n===== VALIDATING TOP-3 ON {args.validate} DAYS =====")
        for r in good[:3]:
            params = {k: r[k] for k in ("wick_atr", "min_confirm", "sl_pct", "confirm")}
            v = run_combo(args, {**params, "days": args.validate}) if False else None
            # run with the validation window
            cmd = ["python", "scripts/backtest_csv.py", "--tf", "5", "--days", str(args.validate),
                   "--db", f"data/val_{abs(hash(frozenset(params.items()))) % 10**6}.db"]
            FLAG = {"wick_atr": "--wick", "min_confirm": "--min-confirm",
                    "sl_pct": "--sl-pct", "confirm": "--confirm", "break_atr": "--break"}
            for k, val in params.items():
                cmd += [FLAG.get(k, f"--{k}"), str(val)]
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=args.timeout)
            out = p.stdout
            def g2(pattern, default=0.0):
                m = re.search(pattern, out)
                return float(m.group(1)) if m else default
            t = int(g2("trades: (\d+)", 0))
            wr = g2("win rate: ([\d.]+)%")
            pnl = g2("gross P&L: INR ([-+\d,]+)")
            pf = g2("profit factor: ([\d.]+)")
            print(f"  {params} -> trades={t} wr={wr:.1f}% pnl={pnl:,.0f} pf={pf:.2f}")


if __name__ == "__main__":
    main()
