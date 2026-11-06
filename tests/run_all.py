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

# Scripts that assert properties of the shipped repository rather than of the
# running application. They must run before the other scripts, which leave test
# accounts, packs and sessions in the database they inspect.
_GUARD_SCRIPTS = frozenset({"verify_secrets.py"})

sys.path.insert(0, str(ROOT))

from modules import run_log  # noqa: E402  (path setup must come first)


def _scripts(filters: Sequence[str]) -> List[Path]:
    """Return test scripts in ``tests/`` matching any name filter.

    Args:
        filters (Sequence[str]): Substrings matched against the file name. An
            empty sequence selects every ``*.py`` script except this runner and
            the ``_``-prefixed helper modules (``_seed_synthetic.py``), which are
            imported by tests rather than executed on their own.

    Returns:
        List[Path]: Sorted script paths, with the static guards first.

    Example:
        >>> [p.name for p in _scripts(["t1"])]
        ['verify_t1.py']
    """
    scripts = sorted(
        (
            p
            for p in TESTS.glob("*.py")
            if p.name != Path(__file__).name and not p.name.startswith("_")
        ),
        # The static guards inspect the shipped database, so they must run
        # before any other script writes test sessions or scratch packs into it.
        key=lambda p: (p.name not in _GUARD_SCRIPTS, p.name),
    )
    if filters:
        scripts = [
            p for p in scripts if any(f.lower() in p.name.lower() for f in filters)
        ]
    return scripts


def _last_line(text: str) -> str:
    """Return the last non-empty line of captured output, truncated to 300 chars."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:300]


# Accounts the test scripts register for their own scenarios. They must never
# survive a test run: the bundled database ships in the deployment, so a
# leftover ``perm_…`` or ``freshbot_…`` login would show up in the live admin
# console and confuse real users.
_SYNTHETIC_PREFIXES = ("acct_", "user_", "freshbot", "perm_", "repro", "t6_", "smoke")
_SYNTHETIC_NAMES = {"testuser99", "edgebot", "alice", "bob", "rootadmin"}

# Packs the scripts create as scratch spaces (permission matrix, import labs,
# restore copies). Real packs (the built-in sample, uploaded actuarial / BP
# packs) never match these prefixes.
_TEST_PACK_PREFIXES = (
    "perm-user-ontology-",
    "perm-admin-ontology-",
    "t5-import-lab",
    "t6-empty-lab",
    "restored-copy",
    "lapse_clean_verify",
)

# The shipped sample pack is identified by slug as well as the ``is_builtin``
# flag, because older databases were seeded before that flag existed.
_SAMPLE_PACK_SLUG = "vnb-ape-sample"

# Everything that references a pack, in an order where each row goes before the
# row it points at. ``ON DELETE CASCADE`` alone is not enough: ``datasets`` is
# reached by two cascade paths (pack and table), and SQLite aborts the whole
# delete with "FOREIGN KEY constraint failed" instead of resolving them. The
# tables below have no declared foreign key, so they are cleared explicitly.
_PACK_DEPENDENTS = (
    ("pack_rows", "dataset_id IN (SELECT id FROM datasets WHERE pack_id IN ({}))"),
    ("pack_columns", "table_id IN (SELECT id FROM pack_tables WHERE pack_id IN ({}))"),
    ("datasets", "pack_id IN ({})"),
    ("pack_tables", "pack_id IN ({})"),
    ("graph_edges", "pack_id IN ({})"),
    ("graph_nodes", "pack_id IN ({})"),
    ("attributes", "pack_id IN ({})"),
    ("relations", "pack_id IN ({})"),
    ("entities", "pack_id IN ({})"),
    ("metric_formulas", "pack_id IN ({})"),
    ("question_rules", "pack_id IN ({})"),
    ("question_bank", "pack_id IN ({})"),
    ("pack_versions", "pack_id IN ({})"),
    ("self_iter_findings", "pack_id IN ({})"),
    ("self_iter_sessions", "pack_id IN ({})"),
    ("ontology_proposals", "pack_id IN ({})"),
    ("studio_history", "pack_id IN ({})"),
    ("user_questions", "pack_id IN ({})"),
    ("ask_history", "pack_id IN ({})"),
    ("ontology_packs", "id IN ({})"),
)

# Rows whose parent was deleted without cascading (an interrupted run, or an
# older schema without the constraint). They would otherwise make the next
# pack delete fail on the very same foreign key.
_ORPHAN_DELETES = (
    ("pack_rows", "dataset_id NOT IN (SELECT id FROM datasets)"),
    ("pack_columns", "table_id NOT IN (SELECT id FROM pack_tables)"),
    ("datasets", "table_id NOT IN (SELECT id FROM pack_tables)"),
    ("pack_tables", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("graph_edges", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("attributes", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("entities", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("relations", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("metric_formulas", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("question_rules", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("question_bank", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("pack_versions", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("self_iter_findings", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("self_iter_sessions", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("ontology_proposals", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("studio_history", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("user_questions", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("ask_history", "pack_id NOT IN (SELECT id FROM ontology_packs)"),
    ("reports", "owner_id NOT IN (SELECT id FROM users)"),
    ("sessions", "user_id NOT IN (SELECT id FROM users)"),
)


def _sweep_orphans(conn) -> None:
    """Delete rows whose parent row no longer exists.

    Args:
        conn: Open connection to the application database. The caller owns the
            transaction.

    Example:
        >>> _sweep_orphans(conn)  # doctest: +SKIP
    """
    for table, where in _ORPHAN_DELETES:
        conn.execute(f"DELETE FROM {table} WHERE {where}")


def _clean_test_accounts() -> int:
    """Delete throw-away accounts created by the test scripts.

    Only names that match the synthetic patterns are removed, so real
    accounts (``admin``, ``giteam``, ``ais123``, …) are never touched.

    Returns:
        int: Number of accounts deleted.

    Example:
        >>> _clean_test_accounts()  # doctest: +SKIP
        3
    """
    from modules.db import get_conn

    with get_conn() as conn:
        rows = conn.execute("SELECT id, username FROM users").fetchall()
        drop = [
            row["id"]
            for row in rows
            if row["username"] in _SYNTHETIC_NAMES
            or row["username"].startswith(_SYNTHETIC_PREFIXES)
        ]
        if drop:
            marks = ",".join("?" * len(drop))
            conn.execute(f"DELETE FROM users WHERE id IN ({marks})", drop)
        # The bundled database ships in the repository, so it must never carry
        # a live session: whoever held the cookie would inherit that account.
        # Sessions are cheap to recreate — every user simply signs in again.
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM revoked_sessions")
        _sweep_orphans(conn)
    return len(drop)


def _clean_test_packs() -> int:
    """Delete scratch packs left behind by the test scripts.

    Children are removed explicitly in dependency order rather than relying on
    ``ON DELETE CASCADE``, which fails here because ``datasets`` is reachable
    through two cascade paths at once. The admin console and the shipped
    database then contain only real ontologies after a run.

    Returns:
        int: Number of packs deleted.

    Example:
        >>> _clean_test_packs()  # doctest: +SKIP
        12
    """
    from modules.db import get_conn

    with get_conn() as conn:
        _sweep_orphans(conn)
        rows = conn.execute(
            "SELECT id, slug, is_active FROM ontology_packs ORDER BY id"
        ).fetchall()
        drop = [r["id"] for r in rows if r["slug"].startswith(_TEST_PACK_PREFIXES)]
        if not drop:
            return 0
        if any(r["is_active"] for r in rows if r["id"] in drop):
            builtin = conn.execute(
                "SELECT id FROM ontology_packs "
                "WHERE is_builtin=1 OR slug=? ORDER BY id LIMIT 1",
                (_SAMPLE_PACK_SLUG,),
            ).fetchone()
            if builtin is None:
                return 0
            conn.execute("UPDATE ontology_packs SET is_active=0")
            conn.execute(
                "UPDATE ontology_packs SET is_active=1 WHERE id=?", (builtin["id"],)
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) "
                "VALUES ('active_pack_id', ?)",
                (str(builtin["id"]),),
            )
        marks = ",".join("?" * len(drop))
        for table, where in _PACK_DEPENDENTS:
            conn.execute(f"DELETE FROM {table} WHERE {where.format(marks)}", drop)
        conn.execute(
            f"DELETE FROM app_settings WHERE key IN ({','.join('?' * len(drop))})",
            [f"analysis:{pid}" for pid in drop],
        )
    return len(drop)


def _restore_seed_datasets() -> int:
    """Drop test uploads from the built-in pack and re-activate its seed data.

    The upload tests push scratch CSVs/XLSXs into the sample pack's tables.
    Leaving them active changes what the demo computes, so every built-in
    table is pointed back at its ``kind='seed'`` dataset.

    Returns:
        int: Number of uploaded datasets removed.

    Example:
        >>> _restore_seed_datasets()  # doctest: +SKIP
        4
    """
    from modules.db import get_conn

    with get_conn() as conn:
        sample = "SELECT id FROM ontology_packs WHERE is_builtin=1 OR slug=?"
        slug = (_SAMPLE_PACK_SLUG,)
        uploaded = conn.execute(
            "SELECT COUNT(*) n FROM datasets WHERE kind <> 'seed' "
            f"AND pack_id IN ({sample})",
            slug,
        ).fetchone()["n"]
        conn.execute(
            f"DELETE FROM datasets WHERE kind <> 'seed' AND pack_id IN ({sample})",
            slug,
        )
        conn.execute(
            "UPDATE pack_tables SET active_dataset_id = ("
            "  SELECT d.id FROM datasets d "
            "  WHERE d.table_id = pack_tables.id AND d.kind = 'seed' "
            "  ORDER BY d.id LIMIT 1"
            ") "
            f"WHERE pack_id IN ({sample}) "
            "AND EXISTS (SELECT 1 FROM datasets d "
            "            WHERE d.table_id = pack_tables.id AND d.kind = 'seed') "
            "AND active_dataset_id NOT IN "
            "    (SELECT id FROM datasets WHERE kind = 'seed')",
            slug,
        )
        conn.execute("DELETE FROM sessions WHERE user_id NOT IN (SELECT id FROM users)")
    return uploaded


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
    # Start from a clean workspace: a previous run (or a half-finished one)
    # may have left scratch accounts or packs that would make today's run fail.
    _clean_test_accounts()
    _clean_test_packs()
    _restore_seed_datasets()
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
    removed_users = _clean_test_accounts()
    removed_packs = _clean_test_packs()
    removed_datasets = _restore_seed_datasets()
    print(
        f"Test state cleaned: {removed_users} accounts, "
        f"{removed_packs} packs, {removed_datasets} datasets"
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
