"""Seed the four Deloitte shared workspace accounts.

Run from the repo root (``python scripts/seed_preset_accounts.py``).
Idempotent: existing rows are skipped, so re-running after a schema
upgrade is safe.

These are **shared team accounts** — no personal e-mail is bound at seed
time. The team takes the initial password and either binds an
Authenticator app (recommended, no network) or a shared mailbox the
first time anyone signs in.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules import auth  # noqa: E402
from modules.db import get_conn, init_db  # noqa: E402

PRESET_PASSWORD = "OntiDeloitte@2026"

ACCOUNTS = [
    ("dais_hk_life", "Deloitte AI Solutions — HK Life Team"),
    ("dais_hk_gi", "Deloitte AI Solutions — HK General Insurance Team"),
    ("dais_ml_life", "Deloitte AI Solutions — Mainland Life Team"),
    ("dais_ml_gi", "Deloitte AI Solutions — Mainland General Insurance Team"),
]


def main() -> int:
    init_db(seed=False)
    created = 0
    skipped = 0
    with get_conn() as conn:
        for username, display_name in ACCOUNTS:
            existing = conn.execute(
                "SELECT id FROM users WHERE username=?",
                (username,),
            ).fetchone()
            if existing is not None:
                skipped += 1
                print(f"  skip {username} — already exists")
                continue
            auth.register_account(
                conn,
                username,
                PRESET_PASSWORD,
                display_name=display_name,
                ip="seed",
            )
            # register_account expects the first argument to be an e-mail
            # address; for shared accounts we want no e-mail at all, so
            # clear the field and the verified flag right after insert.
            conn.execute(
                "UPDATE users SET email='', email_verified=0, role='user' "
                "WHERE username=?",
                (username,),
            )
            created += 1
            print(f"  + created {username} (shared — no bound e-mail)")
    print(
        f"\nDone: {created} new, {skipped} skipped. "
        f"Initial password for all four: {PRESET_PASSWORD}"
    )
    print(
        "Each account is shared. The team's first sign-in is asked to either "
        "bind an Authenticator app (no network, recommended) or a shared mailbox."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
