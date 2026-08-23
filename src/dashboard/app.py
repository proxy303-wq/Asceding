"""FastAPI dashboard: live portfolio view over the bridge state + SQLite."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from src import bridge  # noqa: E402
from src.config import load_config  # noqa: E402
from src.db.store import Store  # noqa: E402

cfg = load_config()
store = Store(cfg.get("db_path", "data/trader.db"))
app = FastAPI(title="dhan-auto-trader dashboard")

STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"))


@app.get("/api/state")
def api_state():
    s = bridge.read_state()
    if not s:
        return {"running": False, "message": "No state yet - start the trading loop (scripts/paper_run.py)"}
    s["running"] = True
    return s


@app.get("/api/trades")
def api_trades(limit: int = 100):
    return {"trades": store.all_trades(limit)}


@app.get("/api/signals")
def api_signals(limit: int = 50):
    return {"signals": store.recent_signals(limit)}


@app.post("/api/control")
def api_control(payload: dict):
    auto = payload.get("auto")
    mode = payload.get("mode")
    if auto is not None:
        bridge.set_control({"auto": bool(auto)})
    if mode in ("paper", "live"):
        bridge.set_control({"mode": mode})
    return {"ok": True, "auto": auto, "mode": mode}
