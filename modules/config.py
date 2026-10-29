"""Central configuration loader for the Onti platform."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "config.json"

with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
    CONFIG: Dict[str, Any] = json.load(_f)

SECURITY = CONFIG.get("security", {})

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
