"""Seed the four Deloitte Onti workspace accounts.

Run from the repo root (``python scripts/seed_preset_accounts.py``).
Idempotent: existing rows are skipped, so re-running after a schema
upgrade is safe. All preset accounts are created with e-mail addresses
following the ``<username>@deloitte.com.hk`` convention, MFA disabled by
default and ready to bind an Authenticator app from the account page.
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
    ("dais_hk_life", "dais_hk_life@deloitte.com.hk", "DAIS HK Life Team"),
    ("dais_hk_gi", "dais_hk_gi@deloitte.com.hk", "DAIS HK General Insurance Team"),
    ("dais_ml_life", "dais_ml_life@deloitte.com.hk", "DAIS ML Life Team"),
    ("dais_ml_gi", "dais_ml_gi@deloitte.com.hk", "DAIS ML General Insurance Team"),
]


def main() -> int:
    init_db(seed=False)
    created = 0
    skipped = 0
    with get_conn() as conn:
        for username, email, display_name in ACCOUNTS:
            existing = conn.execute(
                "SELECT id FROM users WHERE username=? OR email=?",
                (username, email),
            ).fetchone()
            if existing is not None:
                skipped += 1
                print(f"  skip {username} ({email}) — already exists")
                continue
            auth.register_account(
                conn,
                email,
                PRESET_PASSWORD,
                display_name=display_name,
                ip="seed",
            )
            # The flow above registers the e-mail and marks it verified —
            # register_account sets ``email_verified=1`` as a side effect
            # because it is the "admin-trusted" path. Confirm that and also
            # flip role to user (register_account makes the first row admin,
            # so this guard is moot here but keeps things honest if the seed
            # is run after the admin already exists).
            conn.execute("UPDATE users SET role='user' WHERE username=?", (username,))
            created += 1
            print(f"  + created {username} ({email})")
    print(f"\nDone: {created} new, {skipped} skipped. Password: {PRESET_PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
