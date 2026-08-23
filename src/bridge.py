"""Bridge between the trading loop and external clients (MCP server, dashboard).

State and control are exchanged through small JSON files under data/ so the MCP
server and dashboard can run as separate processes without locking.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = DATA_DIR / "state.json"
MANUAL_ORDERS = DATA_DIR / "manual_orders.json"
CONTROL_FILE = DATA_DIR / "control.json"


def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("read %s failed: %s", path, e)
    return default


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


# ---------------- loop -> clients ----------------
def publish_state(state: dict):
    state["ts"] = time.time()
    _write_json(STATE_FILE, state)


def read_state() -> dict:
    return _read_json(STATE_FILE, {})


# ---------------- clients -> loop ----------------
def append_manual_order(order: dict) -> dict:
    orders = _read_json(MANUAL_ORDERS, {"orders": []})
    order["id"] = "M" + str(int(time.time() * 1000))
    order["ts"] = time.time()
    order["status"] = "QUEUED"
    orders.setdefault("orders", []).append(order)
    _write_json(MANUAL_ORDERS, orders)
    return order


def poll_manual_orders() -> list[dict]:
    orders = _read_json(MANUAL_ORDERS, {"orders": []})
    out = [o for o in orders.get("orders", []) if o.get("status") == "QUEUED"]
    for o in out:
        o["status"] = "EXECUTING"
    _write_json(MANUAL_ORDERS, orders)
    return out


def mark_manual_order(order_id: str, ok: bool, msg: str = ""):
    orders = _read_json(MANUAL_ORDERS, {"orders": []})
    for o in orders.get("orders", []):
        if o.get("id") == order_id:
            o["status"] = "EXECUTED" if ok else "FAILED"
            o["message"] = msg
            break
    _write_json(MANUAL_ORDERS, orders)


# ---------------- control ----------------
def set_control(control: dict):
    cur = read_control()
    cur.update(control)
    _write_json(CONTROL_FILE, cur)


def read_control() -> dict:
    return _read_json(CONTROL_FILE, {"auto": True, "note": ""})
