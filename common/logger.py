"""Lightweight application logger.

Provides a single ``AppLogger`` with timestamped console output, kept
dependency-free so the Onti platform can run on a stock Python install.
"""

from __future__ import annotations

import sys
from datetime import datetime


class AppLogger:
    """Minimal leveled logger that writes formatted lines to stdout/stderr."""

    LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

    def __init__(self, name: str = "onti", level: str = "INFO"):
        self.name = name
        self.level = self.LEVELS.get(level.upper(), 20)

    def _emit(self, level: str, color: str, message: str) -> None:
        if self.LEVELS[level] < self.level:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reset = "\033[0m"
        stream = sys.stderr if level in ("WARN", "ERROR") else sys.stdout
        print(f"{color}[{ts}] [{level:5}] [{self.name}] {message}{reset}", file=stream)

    def debug(self, message: str) -> None:
        self._emit("DEBUG", "\033[90m", message)

    def info(self, message: str) -> None:
        self._emit("INFO", "\033[36m", message)

    def warn(self, message: str) -> None:
        self._emit("WARN", "\033[33m", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", "\033[31m", message)
