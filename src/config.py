"""Configuration loader: config.yaml + environment overrides (incl. .env files)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"

# .env files are loaded in order (later files override earlier ones, but never
# override variables already set in the process environment).
ENV_FILES = [
    ROOT / ".env",
    Path("C:/Athena_X/.env"),   # existing ATHENA-X credentials file
    Path("C:/Athena_X/.env.example"),
]


def _load_dotenv_files():
    for env_file in ENV_FILES:
        try:
            if not env_file.exists():
                continue
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except Exception:
            continue


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    _load_dotenv_files()
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # Environment overrides (credentials never live in yaml)
    cfg["dhan_client_id"] = os.getenv("DHAN_CLIENT_ID", cfg.get("dhan_client_id", ""))
    cfg["dhan_access_token"] = os.getenv("DHAN_ACCESS_TOKEN", cfg.get("dhan_access_token", ""))
    cfg["dhan_pin"] = os.getenv("DHAN_PIN", "")
    cfg["dhan_totp_secret"] = os.getenv("DHAN_TOTP_SECRET", "")
    cfg["dhan_api_key"] = os.getenv("DHAN_API_KEY", "")
    cfg["dhan_api_secret"] = os.getenv("DHAN_API_SECRET", "")

    # 12-month API key/secret file (C:\Athena_X\dhan API KKEY.txt)
    if (not cfg["dhan_api_key"] or not cfg["dhan_api_secret"]) and not os.environ.get("DHAN_API_KEY"):
        import re as _re
        for _p in (Path("C:/Athena_X/dhan API KKEY.txt"),
                   Path("C:/Athena_X/dhan_api_key.txt"), ROOT / "dhan_api_key.txt"):
            try:
                if not _p.exists():
                    continue
                _txt = _p.read_text(encoding="utf-8")
                _m1 = _re.search(r"(?i)api.?key.?[-:].?([A-Za-z0-9-]+)", _txt)
                _m2 = _re.search(r"(?i)api.?secret.?[-:].?([A-Za-z0-9-]+)", _txt)
                if _m1 and _m2:
                    cfg["dhan_api_key"] = _m1.group(1).strip()
                    cfg["dhan_api_secret"] = _m2.group(1).strip()
                    os.environ.setdefault("DHAN_API_KEY", cfg["dhan_api_key"])
                    os.environ.setdefault("DHAN_API_SECRET", cfg["dhan_api_secret"])
                    break
            except Exception:
                continue
    env_mode = os.getenv("TRADING_MODE", "").strip().lower()
    if env_mode in ("paper", "live"):
        cfg["mode"] = env_mode

    # persisted token from a consent flow (scripts/dhan_consent.py)
    if not cfg["dhan_access_token"]:
        _token_file = ROOT / "data" / "dhan_token.txt"
        try:
            if _token_file.exists():
                cfg["dhan_access_token"] = _token_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    # auto-refresh an expired token with TOTP+pin when configured
    if cfg["dhan_access_token"] or (cfg["dhan_pin"] and cfg["dhan_totp_secret"]):
        try:
            from src.broker import auth as _auth
            cfg["dhan_access_token"] = _auth.resolve_token(
                client_id=cfg["dhan_client_id"],
                access_token=cfg["dhan_access_token"],
                pin=cfg["dhan_pin"],
                totp_secret=cfg["dhan_totp_secret"],
                api_key=cfg["dhan_api_key"],
                api_secret=cfg["dhan_api_secret"],
            )
        except Exception:
            pass

    # Paths
    cfg.setdefault("db_path", str(ROOT / "data" / "trader.db"))
    cfg.setdefault("logs_dir", str(ROOT / "logs"))
    return cfg


def market_is_open(now=None) -> bool:
    """NSE cash/F&O hours: Mon-Fri 09:15-15:30 IST."""
    from datetime import datetime, time as dtime
    import zoneinfo

    if now is None:
        now = datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata"))
    if now.weekday() >= 5:
        return False
    open_t, close_t = dtime(9, 15), dtime(15, 30)
    return open_t <= now.time() <= close_t


def market_open_time(now=None):
    from datetime import datetime, time as dtime
    import zoneinfo

    if now is None:
        now = datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata"))
    return datetime.combine(now.date(), dtime(9, 15), tzinfo=now.tzinfo)


def ist_now():
    from datetime import datetime
    import zoneinfo

    return datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata"))
