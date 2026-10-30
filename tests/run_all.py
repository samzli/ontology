"""Run every test script and write an auditable run-log entry per script.

Usage (from the project root, Windows PowerShell)::

    python tests/run_all.py                # run everything
    python tests/run_all.py smoke verify_t1  # run a subset by name filter

Each execution is recorded through :mod:`modules.run_log` with the actor,
start/finish time, duration, exit code and the last line of output, so a
reviewer can tell *who* ran *what*, *when*, and *with which result*.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import List, Sequence

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
TIMEOUT_S = 900

sys.path.insert(0, str(ROOT))

from modules import run_log  # noqa: E402  (path setup must come first)


def _scripts(filters: Sequence[str]) -> List[Path]:
    """Return test scripts in ``tests/`` matching any name filter.

    Args:
        filters (Sequence[str]): Substrings matched against the file name. An
            empty sequence selects every ``*.py`` script except this runner.

    Returns:
        List[Path]: Sorted script paths.

    Example:
        >>> [p.name for p in _scripts(["t1"])]
        ['verify_t1.py']
    """
    scripts = sorted(p for p in TESTS.glob("*.py") if p.name != Path(__file__).name)
    if filters:
        scripts = [
            p for p in scripts if any(f.lower() in p.name.lower() for f in filters)
        ]
    return scripts


def _last_line(text: str) -> str:
    """Return the last non-empty line of captured output, truncated to 300 chars."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:300]


def main(argv: Sequence[str]) -> int:
    """Run the selected scripts, log every result and return an exit code.

    Args:
        argv (Sequence[str]): Command-line arguments after the script name.

    Returns:
        int: ``0`` when every script passed, ``1`` otherwise.

    Example:
        >>> main([])  # doctest: +SKIP
        0
    """
    failures = 0
    for script in _scripts(argv):
        started = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=TIMEOUT_S,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            status = "PASS" if proc.returncode == 0 else "FAIL"
            summary = _last_line(output) or f"exit {proc.returncode}"
            details = {"exit_code": proc.returncode}
        except subprocess.TimeoutExpired:
            status = "FAIL"
            summary = f"timeout after {TIMEOUT_S}s"
            details = {"exit_code": None, "timeout": True}
        run_log.timed(
            f"tests/{script.name}",
            status,
            summary,
            started,
            details=details,
        )
        print(f"{status:<4} {script.name:<28} {summary}")
        failures += 0 if status == "PASS" else 1

    print(f"\nRun log: {run_log.RUN_LOG_PATH}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
