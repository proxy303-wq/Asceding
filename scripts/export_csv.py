"""Export trades and equity curve to CSV for external analysis."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402
from src.db.store import Store  # noqa: E402
from src.config import ist_now  # noqa: E402


def main():
    cfg = load_config()
    store = Store(cfg.get("db_path", "data/trader.db"))
    out = Path("data/export")
    out.mkdir(parents=True, exist_ok=True)

    trades = store.all_trades(100000)
    with open(out / "trades.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "symbol", "underlying", "option_type", "strike", "strategy",
                    "entry_price", "exit_price", "quantity", "pnl", "exit_reason"])
        for t in trades:
            w.writerow([t["ts"], t["symbol"], t["underlying"], t["option_type"], t["strike"],
                        t["strategy"], t["entry_price"], t["exit_price"], t["quantity"],
                        t["pnl"], t["exit_reason"]])
    eq = store.equity_curve(100000)
    with open(out / "equity.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "day", "cash", "unrealized", "equity", "day_pnl", "drawdown_pct"])
        for e in eq:
            w.writerow([e["ts"], e["day"], e["cash"], e["unrealized"], e["equity"],
                        e["day_pnl"], e["drawdown_pct"]])
    print(f"exported {len(trades)} trades -> {out / 'trades.csv'}")
    print(f"exported {len(eq)} equity points -> {out / 'equity.csv'}")


if __name__ == "__main__":
    main()
