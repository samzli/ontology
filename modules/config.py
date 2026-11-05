"""Central configuration loader for the Onti platform.

Configuration lives in ``config/config.json``. Two environment switches make
the same code base deployable to several environments without edits:

* ``ONTI_CONFIG`` — absolute path to a different config file (containers, CI).
* ``ONTI_ENV`` — environment name; when ``config/config.<name>.json`` exists it
  is deep-merged over the base file (``config/config.docker.json``, …).

Secrets never live in these files: tokens are read from the environment
variables named by the ``snapshot`` section.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``overlay`` into ``base`` recursively, overlay winning per key.

    Args:
        base (Dict[str, Any]): Base configuration mapping.
        overlay (Dict[str, Any]): Values that take precedence.

    Returns:
        Dict[str, Any]: A new mapping with nested dictionaries merged.

    Example:
        >>> _deep_merge({"a": {"b": 1}}, {"a": {"c": 2}})
        {'a': {'b': 1, 'c': 2}}
    """
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load() -> Dict[str, Any]:
    """Load the base config and apply the optional environment overlay.

    Returns:
        Dict[str, Any]: Effective configuration.

    Raises:
        FileNotFoundError: When the base (or ``ONTI_CONFIG``) file is missing.
        json.JSONDecodeError: When a config file is not valid JSON.
    """
    override = os.environ.get("ONTI_CONFIG", "").strip()
    path = (
        Path(override).expanduser().resolve()
        if override
        else CONFIG_DIR / "config.json"
    )
    with open(path, "r", encoding="utf-8") as f:
        config: Dict[str, Any] = json.load(f)
    env = os.environ.get("ONTI_ENV", "").strip()
    overlay = CONFIG_DIR / f"config.{env}.json" if env else None
    if overlay is not None and overlay.exists():
        with open(overlay, "r", encoding="utf-8") as f:
            config = _deep_merge(config, json.load(f))
    return config


CONFIG: Dict[str, Any] = _load()
CONFIG_PATH = Path(
    os.environ.get("ONTI_CONFIG", "").strip() or CONFIG_DIR / "config.json"
).resolve()

SECURITY = CONFIG.get("security", {})
RUNTIME = CONFIG.get("runtime", {})
SNAPSHOT = CONFIG.get("snapshot", {})

SYSTEM_NAME = CONFIG.get("system_name", "Onti")
VERSION = CONFIG.get("version", "2.0.0")
PORT = CONFIG.get("server", {}).get("port", 8010)
HOST = CONFIG.get("server", {}).get("host", "0.0.0.0")

SESSION_DAYS = int(SECURITY.get("session_days", 7))
UPLOAD_MAX_MB = int(SECURITY.get("upload_max_mb", 10))
ALLOWED_UPLOAD_EXT = [
    e.lower()
    for e in SECURITY.get(
        "allowed_upload_ext", [".csv", ".xlsx", ".xls", ".json", ".txt"]
    )
]
