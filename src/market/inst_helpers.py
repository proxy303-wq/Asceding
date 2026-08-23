"""Underlying config helpers."""
from __future__ import annotations

from typing import Any


def inst_cfg(underlying: str, config: dict, key: str, default: Any = None) -> Any:
    """Look up an instrument-level config value (e.g. strike_interval) from the
    combined instrument section passed in context.config."""
    if isinstance(config, dict):
        return config.get(key, default)
    return default
