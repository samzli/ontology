"""Run log — append-only audit trail for test and maintenance runs.

Every entry answers three questions: *who* ran something, *when* it ran and
*what* was executed and with which outcome. Records are written as one JSON
object per line so the file can be tailed, grepped or shipped to a log
collector without a database.

The location and the maximum number of retained entries are configuration
values (``runtime.run_log`` / ``runtime.run_log_max_entries`` in
``config/config.json``), never hard-coded at the call site.
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules import config as cfg

_RUNTIME = cfg.CONFIG.get("runtime", {})
RUN_LOG_PATH = Path(_RUNTIME.get("run_log", "data/run_log.jsonl"))
RUN_LOG_MAX_ENTRIES = int(_RUNTIME.get("run_log_max_entries", 5000))


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _actor() -> str:
    """Return the best available identifier for the current OS user."""
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - exotic environments
        return os.environ.get("USERNAME") or "unknown"


def record_run(
    script: str,
    status: str,
    summary: str = "",
    *,
    actor: Optional[str] = None,
    started_at: Optional[str] = None,
    duration_ms: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append one run record and return the stored entry.

    Args:
        script (str): Command or test module that was executed, e.g.
            ``tests/smoke_http.py``.
        status (str): Outcome label such as ``PASS``, ``FAIL`` or ``SKIP``.
        summary (str): One-line human-readable result description.
        actor (Optional[str]): Who ran it. Defaults to the OS user name.
        started_at (Optional[str]): ISO timestamp of the start; defaults to now.
        duration_ms (Optional[int]): Wall-clock duration in milliseconds.
        details (Optional[Dict[str, Any]]): Extra structured fields (counts,
            command line, exit code).

    Returns:
        Dict[str, Any]: The entry that was written to the log.

    Raises:
        OSError: If the log file cannot be created or appended.

    Example:
        >>> record_run("tests/verify_t1.py", "PASS", "12 checks", duration_ms=840)
        {'run_id': '...', 'script': 'tests/verify_t1.py', 'status': 'PASS', ...}
    """
    entry: Dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:12],
        "started_at": started_at or _now(),
        "finished_at": _now(),
        "actor": actor or _actor(),
        "host": socket.gethostname(),
        "script": script,
        "status": status.upper(),
        "summary": summary,
        "duration_ms": duration_ms,
        "details": details or {},
    }
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _trim()
    return entry


def _trim() -> None:
    """Keep the log bounded by ``runtime.run_log_max_entries`` (best effort)."""
    if RUN_LOG_MAX_ENTRIES <= 0 or not RUN_LOG_PATH.exists():
        return
    lines = RUN_LOG_PATH.read_text(encoding="utf-8").splitlines()
    if len(lines) <= RUN_LOG_MAX_ENTRIES:
        return
    tmp = RUN_LOG_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines[-RUN_LOG_MAX_ENTRIES:]) + "\n", encoding="utf-8")
    tmp.replace(RUN_LOG_PATH)


def list_runs(limit: int = 20) -> List[Dict[str, Any]]:
    """Return the most recent run records, newest last.

    Args:
        limit (int): Maximum number of records to return.

    Returns:
        List[Dict[str, Any]]: Parsed log entries; malformed lines are skipped.

    Example:
        >>> list_runs(1)[0]["status"]
        'PASS'
    """
    if not RUN_LOG_PATH.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for line in RUN_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def timed(
    script: str, status: str, summary: str, started: float, **kwargs: Any
) -> Dict[str, Any]:
    """Record a run whose duration is measured from ``time.time()``.

    Args:
        script (str): Command or test module that was executed.
        status (str): Outcome label (``PASS`` / ``FAIL`` / ``SKIP``).
        summary (str): One-line result description.
        started (float): Value returned by ``time.time()`` before the run.
        **kwargs (Any): Forwarded to :func:`record_run` (actor, details...).

    Returns:
        Dict[str, Any]: The stored log entry.

    Example:
        >>> t0 = time.time(); ...; timed("tests/verify_t2.py", "PASS", "ok", t0)
    """
    return record_run(
        script,
        status,
        summary,
        started_at=datetime.fromtimestamp(started, timezone.utc).isoformat(
            timespec="seconds"
        ),
        duration_ms=int((time.time() - started) * 1000),
        **kwargs,
    )


if __name__ == "__main__":
    for item in list_runs(10):
        print(
            f"{item['finished_at']}  {item['status']:<4} {item['actor']:<12} {item['script']}  {item['summary']}"
        )
