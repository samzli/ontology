"""Secret-hygiene regression — nothing sensitive may live in the repository.

Guards the incident that GitGuardian raised against this public repository:
the machine master key (``data/.onti.key``) was committed next to
``data/onti.db``, so anyone could decrypt the model API keys stored in that
database and forge session cookies for the deployed instance.

The checks are deliberately independent of the application runtime: they read
the git index, the ignore rules and the shipped database directly, so they
fail even when the code paths that use those secrets still work.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0

# High-confidence credential shapes. Deliberately narrow so ordinary prose and
# placeholder examples ("your-api-key") never trip the scan.
SECRET_PATTERNS = [
    ("openai-style", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("ak-style", re.compile(r"\bak-[A-Za-z0-9]{16,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("vercel-token", re.compile(r"\b[A-Za-z0-9]{24}_[A-Za-z0-9]{20,}\b")),
]

TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".html",
    ".json",
    ".md",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
    ".example",
    ".bat",
    ".css",
}

# Files that legitimately contain example credentials.
ALLOWED_FILES = {".env.example"}


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ FAIL {label}  :: {detail}")


def _git(*args: str) -> str:
    """Run a git command in the repository and return stdout."""
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def main() -> int:
    print("=== Secret hygiene regression ===")

    # [1] The machine key must never be tracked again.
    tracked_key = [
        line
        for line in _git("ls-files", "data").splitlines()
        if line.strip().endswith(".onti.key")
    ]
    ok(tracked_key == [], "[1] data/.onti.key is not tracked", tracked_key)

    # [2] The ignore rules cover the key file, including future copies.
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    ok("data/.onti.key" in ignore, "[2] .gitignore lists data/.onti.key")
    ok("*.onti.key" in ignore, "[2] .gitignore covers any *.onti.key copy")

    # [3] The shipped database holds no credential that a shared key opens.
    db_path = ROOT / "data" / "onti.db"
    conn = sqlite3.connect(db_path)
    try:
        stored = conn.execute(
            "SELECT COUNT(*) FROM model_configs WHERE COALESCE(api_key_enc,'') <> ''"
        ).fetchone()[0]
        ok(stored == 0, "[3] bundled DB ships no encrypted model secret", stored)
        try:
            user_stored = conn.execute(
                "SELECT COUNT(*) FROM user_model_configs"
                " WHERE COALESCE(api_key_enc,'') <> ''"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            user_stored = 0
        ok(
            user_stored == 0,
            "[3] bundled DB ships no per-user model secret",
            user_stored,
        )
        live_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        ok(live_sessions == 0, "[3] bundled DB ships no live session", live_sessions)
    finally:
        conn.close()

    # [4] No tracked text file carries a real-looking credential.
    hits = []
    for rel in _git("ls-files").splitlines():
        path = ROOT / rel
        if path.suffix.lower() not in TEXT_SUFFIXES or path.name in ALLOWED_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                hits.append(f"{rel}: {name}")
    ok(hits == [], "[4] no credential-shaped string in tracked files", hits[:5])

    # [5] The seeded administrator password is a published, well-known value,
    # so the documentation must tell operators to replace it.
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    ok(
        "default password" in readme and "change" in readme,
        "[5] README tells operators to change the default password",
    )

    print(f"\nSecret hygiene: {PASS} ok / {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
