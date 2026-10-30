"""Lightweight application logger.

Provides a single ``AppLogger`` with timestamped console output, kept
dependency-free so the Onti platform can run on a stock Python install.
"""

from __future__ import annotations

import sys
from datetime import datetime


class AppLogger:
    """Minimal leveled logger that writes formatted lines to stdout/stderr.

    ``DEBUG`` and ``INFO`` records go to stdout, ``WARN`` and ``ERROR`` to
    stderr, so a supervisor that captures the streams separately can alert on
    problems without parsing the log text.

    Example:
        >>> logger = AppLogger("onti", "DEBUG")
        >>> logger.info("server started")
        [2026-10-30 09:00:00] [INFO ] [onti] server started
    """

    LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

    def __init__(self, name: str = "onti", level: str = "INFO"):
        """Create a logger.

        Args:
            name (str): Component name printed in the ``[name]`` slot.
            level (str): Minimum level to emit (``DEBUG`` / ``INFO`` /
                ``WARN`` / ``ERROR``). Unknown values fall back to ``INFO``.
        """
        self.name = name
        self.level = self.LEVELS.get(level.upper(), 20)

    def _emit(self, level: str, color: str, message: str) -> None:
        """Write one formatted line when ``level`` passes the threshold.

        Args:
            level (str): Level key used for both the threshold check and the
                printed label.
            color (str): ANSI escape sequence applied to the line.
            message (str): Text to print.
        """
        if self.LEVELS[level] < self.level:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reset = "\033[0m"
        stream = sys.stderr if level in ("WARN", "ERROR") else sys.stdout
        print(f"{color}[{ts}] [{level:5}] [{self.name}] {message}{reset}", file=stream)

    def debug(self, message: str) -> None:
        """Log a ``DEBUG`` message.

        Args:
            message (str): Text to log.
        """
        self._emit("DEBUG", "\033[90m", message)

    def info(self, message: str) -> None:
        """Log an ``INFO`` message.

        Args:
            message (str): Text to log.
        """
        self._emit("INFO", "\033[36m", message)

    def warn(self, message: str) -> None:
        """Log a ``WARN`` message to stderr.

        Args:
            message (str): Text to log.
        """
        self._emit("WARN", "\033[33m", message)

    def error(self, message: str) -> None:
        """Log an ``ERROR`` message to stderr.

        Args:
            message (str): Text to log.
        """
        self._emit("ERROR", "\033[31m", message)
