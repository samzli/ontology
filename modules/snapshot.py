"""Database snapshot sync for ephemeral (serverless) hosts.

Serverless filesystems are wiped on every cold start, so accounts registered
on one instance would vanish for the next. This module mirrors the SQLite
database to an external store and restores it on boot:

  * **Vercel Blob** — set ``BLOB_READ_WRITE_TOKEN`` (Vercel does this
    automatically when a Blob store is connected to the project).
  * **Any HTTP endpoint you own** — set ``ONTI_SNAPSHOT_URL`` and
    ``ONTI_SNAPSHOT_TOKEN``; the snapshot is ``PUT`` there and fetched back
    with a ``GET`` (a small VPS, an S3 pre-signed pair, a NAS webdav share...).

With no configuration the module is inert: every call is a cheap no-op, so
local and Docker runs behave exactly as before. All network errors are
swallowed and logged — persistence must never break the request path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from common.logger import AppLogger
from modules import config as cfg

_LOG = AppLogger("snapshot")

# Every tunable comes from config/config.json (``snapshot`` section) so hosts
# can adjust timeouts and the Blob prefix without touching code; secrets stay
# in environment variables named there.
_SNAP = cfg.SNAPSHOT
_TIMEOUT = float(_SNAP.get("timeout_s", 20))
# Collapse bursts of account changes (e.g. an admin creating several users)
# into a single upload.
_MIN_INTERVAL_S = float(_SNAP.get("min_interval_s", 5))
_BLOB_API = str(_SNAP.get("blob_api_base", "https://blob.vercel-storage.com"))
_BLOB_API_VERSION = str(_SNAP.get("blob_api_version", "7"))
_PREFIX = str(_SNAP.get("prefix", "onti")).strip("/")
_BLOB_TOKEN_ENV = str(_SNAP.get("blob_token_env", "BLOB_READ_WRITE_TOKEN"))
_HTTP_URL_ENV = str(_SNAP.get("http_url_env", "ONTI_SNAPSHOT_URL"))
_HTTP_TOKEN_ENV = str(_SNAP.get("http_token_env", "ONTI_SNAPSHOT_TOKEN"))

_last_sync = 0.0


def _env(name: str) -> str:
    """Return a trimmed environment variable, or ``""`` when unset."""
    return os.environ.get(name, "").strip()


def backend() -> str:
    """Name the configured snapshot backend.

    Returns:
        str: ``"blob"``, ``"http"`` or ``""`` (sync disabled).

    Example:
        >>> backend() in ("blob", "http", "")
        True
    """
    if _env(_BLOB_TOKEN_ENV):
        return "blob"
    if _env(_HTTP_URL_ENV):
        return "http"
    return ""


def enabled() -> bool:
    """Whether a snapshot backend is configured.

    Returns:
        bool: ``True`` when snapshots can be uploaded and restored.

    Example:
        >>> enabled()
        False
    """
    return backend() != ""


def _namespace() -> str:
    """Secret, stable pathname segment derived from the deployment key.

    Vercel Blob URLs are public, so the snapshot lives under an unguessable
    prefix derived from the machine key instead of a well-known path.

    Returns:
        str: 24 hex characters identifying this deployment's snapshot folder.
    """
    from modules import db  # local import: db imports us lazily

    return hmac.new(db.session_signing_key(), b"snapshot-path", "sha256").hexdigest()[
        :24
    ]


def _db_bytes() -> bytes:
    """Read a consistent copy of the live database.

    Uses SQLite's online backup API instead of copying the file or forcing a
    WAL checkpoint: the backup sees a committed snapshot even while the app
    is writing, so no ``-wal``/``-shm`` sidecar ever has to be shipped.

    Returns:
        bytes: Raw SQLite file contents.

    Raises:
        sqlite3.Error: When the database cannot be opened or backed up.
    """
    from modules import db

    fd, name = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp = Path(name)
    try:
        src = sqlite3.connect(str(db.DB_PATH), timeout=30)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def _looks_like_sqlite(data: bytes) -> bool:
    """Check the SQLite magic header before trusting downloaded bytes.

    Args:
        data (bytes): Candidate database contents.

    Returns:
        bool: ``True`` when the bytes start with the SQLite file magic.
    """
    return data[:16] == b"SQLite format 3\x00"


def _blob_headers() -> dict:
    """Authorization headers shared by every Vercel Blob call."""
    return {
        "authorization": f"Bearer {_env(_BLOB_TOKEN_ENV)}",
        "x-api-version": _BLOB_API_VERSION,
    }


def _upload_blob(data: bytes) -> bool:
    """PUT the snapshot to Vercel Blob under the secret prefix.

    Args:
        data (bytes): Database contents.

    Returns:
        bool: ``True`` on HTTP 2xx.
    """
    path = f"{_PREFIX}/{_namespace()}/onti.db"
    resp = httpx.put(
        f"{_BLOB_API}/{path}",
        content=data,
        headers={
            **_blob_headers(),
            "x-content-type": "application/octet-stream",
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "1",
        },
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 300:
        _LOG.warn(f"blob upload failed: {resp.status_code} {resp.text[:200]}")
        return False
    return True


def _download_blob() -> Optional[bytes]:
    """Fetch the newest snapshot from Vercel Blob.

    Returns:
        Optional[bytes]: Database contents, or ``None`` when no snapshot
            exists yet or the request failed.
    """
    prefix = f"{_PREFIX}/{_namespace()}/"
    resp = httpx.get(
        f"{_BLOB_API}?prefix={prefix}&limit=10",
        headers=_blob_headers(),
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 300:
        _LOG.warn(f"blob list failed: {resp.status_code} {resp.text[:200]}")
        return None
    blobs = resp.json().get("blobs", [])
    if not blobs:
        return None
    latest = max(blobs, key=lambda b: b.get("uploadedAt", ""))
    resp = httpx.get(latest["url"], timeout=_TIMEOUT)
    if resp.status_code >= 300:
        _LOG.warn(f"blob download failed: {resp.status_code}")
        return None
    return resp.content


def _upload_http(data: bytes) -> bool:
    """PUT the snapshot to the operator's own endpoint.

    Args:
        data (bytes): Database contents.

    Returns:
        bool: ``True`` on HTTP 2xx.
    """
    resp = httpx.put(
        _env(_HTTP_URL_ENV),
        content=data,
        headers={"authorization": f"Bearer {_env(_HTTP_TOKEN_ENV)}"},
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 300:
        _LOG.warn(f"http upload failed: {resp.status_code} {resp.text[:200]}")
        return False
    return True


def _download_http() -> Optional[bytes]:
    """GET the snapshot back from the operator's own endpoint.

    Returns:
        Optional[bytes]: Database contents, or ``None`` on failure.
    """
    resp = httpx.get(
        _env(_HTTP_URL_ENV),
        headers={"authorization": f"Bearer {_env(_HTTP_TOKEN_ENV)}"},
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 300:
        return None
    return resp.content


def sync_now() -> bool:
    """Upload a fresh snapshot of the database, best effort.

    Returns:
        bool: ``True`` when a snapshot was uploaded; ``False`` when sync is
            disabled or the upload failed (failures are logged, never raised).

    Example:
        >>> sync_now()
        False
    """
    kind = backend()
    if not kind:
        return False
    try:
        data = _db_bytes()
        ok = _upload_blob(data) if kind == "blob" else _upload_http(data)
        if ok:
            global _last_sync
            _last_sync = time.time()
            _LOG.info(f"snapshot uploaded via {kind} ({len(data)} bytes)")
        return ok
    except Exception as exc:  # noqa: BLE001 - persistence must never break requests
        _LOG.warn(f"snapshot upload skipped: {exc}")
        return False


def request_sync() -> bool:
    """Upload a snapshot unless one just happened (burst collapse).

    Called after account mutations (register / role / password / enable), so
    a registration made on a serverless instance reaches the durable store
    before that instance can be replaced.

    Returns:
        bool: ``True`` when a snapshot was uploaded.

    Example:
        >>> request_sync()
        False
    """
    if not enabled():
        return False
    if time.time() - _last_sync < _MIN_INTERVAL_S:
        return False
    return sync_now()


def restore_latest(target: Path) -> bool:
    """Download the latest snapshot into ``target`` for a cold start.

    Args:
        target (Path): Where the runtime database should live (``DB_PATH`` on
            an ephemeral host).

    Returns:
        bool: ``True`` when a snapshot was restored; ``False`` when sync is
            disabled, no snapshot exists, or the download was invalid — the
            caller then falls back to the bundled database.

    Example:
        >>> restore_latest(Path("/tmp/onti-data/onti.db"))
        False
    """
    kind = backend()
    if not kind:
        return False
    try:
        data = _download_blob() if kind == "blob" else _download_http()
        if not data or not _looks_like_sqlite(data):
            return False
        tmp = target.with_suffix(".restore")
        tmp.write_bytes(data)
        tmp.replace(target)
        for suffix in ("-wal", "-shm"):
            side = Path(str(target) + suffix)
            if side.exists():
                side.unlink()
        _LOG.info(f"snapshot restored via {kind} ({len(data)} bytes)")
        return True
    except Exception as exc:  # noqa: BLE001 - fall back to the bundled database
        _LOG.warn(f"snapshot restore skipped: {exc}")
        return False


# ---------------------------------------------------------------------------
# Account records
# ---------------------------------------------------------------------------
# The whole-database snapshot above is last-writer-wins: two instances that
# booted at different times hold different copies, and whichever uploads last
# replaces the other's registrations. That is exactly how an account could be
# created and then be gone on the next request.
#
# Accounts are therefore mirrored one JSON object per account, keyed by a hash
# of the username. Writing one account can never delete a different one, and
# any instance can fetch a single record when a login arrives for a username
# its own copy does not know.
#
# The path segment is deliberately independent of the master key: rotating the
# key must not orphan accounts created before the rotation. Records hold the
# PBKDF2 hash, never the password, and the store is reached with the operator
# token (Blob listing requires it).

_ACCOUNT_DIR = "shared/accounts"


def account_store() -> bool:
    """Whether account records can be mirrored to a durable store.

    Returns:
        bool: ``True`` when a snapshot backend is configured.

    Example:
        >>> account_store()
        False
    """
    return enabled()


def _account_key(username: str) -> str:
    """Hash a username into a stable, filename-safe key.

    Args:
        username (str): Account name as entered at sign-in.

    Returns:
        str: 64 hex characters, case- and whitespace-insensitive.
    """
    return hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()


def _blob_account_path(username: str) -> str:
    """Pathname of one account record inside the Blob store."""
    return f"{_PREFIX}/{_ACCOUNT_DIR}/{_account_key(username)}.json"


def _http_account_url(username: str) -> str:
    """URL of one account record on the operator's own endpoint."""
    base = _env(_HTTP_URL_ENV)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}account={_account_key(username)}"


def _put_account_blob(username: str, payload: bytes) -> bool:
    """Upload one account record to Vercel Blob (overwrite in place)."""
    resp = httpx.put(
        f"{_BLOB_API}/{_blob_account_path(username)}",
        content=payload,
        headers={
            **_blob_headers(),
            "x-content-type": "application/json",
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "1",
        },
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 300:
        _LOG.warn(f"account upload failed: {resp.status_code} {resp.text[:200]}")
        return False
    return True


def _get_account_blob(username: str) -> Optional[bytes]:
    """Download one account record from Vercel Blob, or ``None``."""
    path = _blob_account_path(username)
    resp = httpx.get(
        f"{_BLOB_API}?prefix={path}&limit=1",
        headers=_blob_headers(),
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 300:
        _LOG.warn(f"account list failed: {resp.status_code} {resp.text[:200]}")
        return None
    for blob in resp.json().get("blobs", []):
        if blob.get("pathname") != path:
            continue
        got = httpx.get(blob["url"], timeout=_TIMEOUT)
        if got.status_code >= 300:
            _LOG.warn(f"account download failed: {got.status_code}")
            return None
        return got.content
    return None


def save_account(record: Dict[str, Any]) -> bool:
    """Persist one account record so every instance can see it.

    Args:
        record (Dict[str, Any]): Row with ``username``, ``display_name``,
            ``password_hash``, ``role``, ``enabled`` and ``created_at``.

    Returns:
        bool: ``True`` when the record was stored (or storage is disabled).

    Example:
        >>> save_account({"username": "alice"})
        False
    """
    kind = backend()
    if not kind:
        return False
    username = str(record.get("username", "")).strip()
    if not username:
        return False
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
    try:
        if kind == "blob":
            return _put_account_blob(username, payload)
        resp = httpx.put(
            _http_account_url(username),
            content=payload,
            headers={
                "authorization": f"Bearer {_env(_HTTP_TOKEN_ENV)}",
                "content-type": "application/json",
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code >= 300:
            _LOG.warn(f"account upload failed: {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - persistence must not break requests
        _LOG.warn(f"account upload skipped: {exc}")
        return False


def load_account(username: str) -> Optional[Dict[str, Any]]:
    """Fetch one account record from the durable store.

    Args:
        username (str): Username supplied at sign-in.

    Returns:
        Optional[Dict[str, Any]]: The stored record, or ``None`` when storage
            is disabled, the record is absent or the download failed.

    Example:
        >>> load_account("alice")  # doctest: +SKIP
        {'username': 'alice', 'role': 'user', ...}
    """
    kind = backend()
    if not kind:
        return None
    try:
        if kind == "blob":
            raw = _get_account_blob(username)
        else:
            resp = httpx.get(
                _http_account_url(username),
                headers={"authorization": f"Bearer {_env(_HTTP_TOKEN_ENV)}"},
                timeout=_TIMEOUT,
            )
            raw = resp.content if resp.status_code < 300 else None
        if not raw:
            return None
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict) or not record.get("username"):
            return None
        return record
    except Exception as exc:  # noqa: BLE001 - a lookup failure is a miss
        _LOG.warn(f"account lookup skipped: {exc}")
        return None


def delete_account(username: str) -> bool:
    """Remove one account record from the durable store.

    Args:
        username (str): Account being deleted.

    Returns:
        bool: ``True`` when the delete was accepted.

    Example:
        >>> delete_account("alice")
        False
    """
    kind = backend()
    if not kind:
        return False
    try:
        if kind == "blob":
            path = _blob_account_path(username)
            resp = httpx.post(
                f"{_BLOB_API}/delete",
                json={"urls": [f"{_BLOB_API}/{path}"]},
                headers=_blob_headers(),
                timeout=_TIMEOUT,
            )
        else:
            resp = httpx.delete(
                _http_account_url(username),
                headers={"authorization": f"Bearer {_env(_HTTP_TOKEN_ENV)}"},
                timeout=_TIMEOUT,
            )
        if resp.status_code >= 300:
            _LOG.warn(f"account delete failed: {resp.status_code}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - deletion is best effort
        _LOG.warn(f"account delete skipped: {exc}")
        return False


def persist_account(conn: sqlite3.Connection, username: str) -> bool:
    """Mirror the current database row for ``username`` to the store.

    Args:
        conn (sqlite3.Connection): Open database connection.
        username (str): Account to mirror.

    Returns:
        bool: ``True`` when the record was stored or storage is disabled.

    Example:
        >>> persist_account(conn, "alice")  # doctest: +SKIP
        True
    """
    if not account_store():
        return True
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row is None:
        return False
    return save_account(
        {
            "username": row["username"],
            "display_name": row["display_name"],
            "password_hash": row["password_hash"],
            "role": row["role"],
            "enabled": int(row["enabled"]),
            "created_at": row["created_at"],
        }
    )


def adopt_account(conn: sqlite3.Connection, record: Dict[str, Any]) -> bool:
    """Insert a remotely stored account into the local database.

    Args:
        conn (sqlite3.Connection): Open database connection.
        record (Dict[str, Any]): Record returned by :func:`load_account`.

    Returns:
        bool: ``True`` when the account is now present locally.

    Example:
        >>> adopt_account(conn, {"username": "alice"})  # doctest: +SKIP
        True
    """
    username = str(record.get("username", "")).strip()
    role = record.get("role") if record.get("role") in ("admin", "user") else "user"
    if not username:
        return False
    conn.execute(
        "INSERT INTO users(username, display_name, password_hash, role, enabled, "
        "created_at) SELECT ?,?,?,?,?,? "
        "WHERE NOT EXISTS (SELECT 1 FROM users WHERE username=?)",
        (
            username,
            record.get("display_name") or username,
            record.get("password_hash") or "",
            role,
            int(record.get("enabled", 1)),
            record.get("created_at") or "",
            username,
        ),
    )
    return (
        conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        is not None
    )


def restore_accounts(conn: sqlite3.Connection) -> int:
    """Pull every stored account record into a cold-started instance.

    Only the Blob backend can be listed; the generic HTTP endpoint exposes one
    record at a time, so it is fetched on demand at sign-in instead.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        int: Number of accounts added locally.

    Example:
        >>> restore_accounts(conn)  # doctest: +SKIP
        2
    """
    if backend() != "blob":
        return 0
    prefix = f"{_PREFIX}/{_ACCOUNT_DIR}/"
    added = 0
    try:
        resp = httpx.get(
            f"{_BLOB_API}?prefix={prefix}&limit=1000",
            headers=_blob_headers(),
            timeout=_TIMEOUT,
        )
        if resp.status_code >= 300:
            _LOG.warn(f"account list failed: {resp.status_code}")
            return 0
        for blob in resp.json().get("blobs", []):
            got = httpx.get(blob["url"], timeout=_TIMEOUT)
            if got.status_code >= 300:
                continue
            try:
                record = json.loads(got.content.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(record, dict) and adopt_account(conn, record):
                added += 1
        if added:
            _LOG.info(f"restored {added} account record(s) from blob")
    except Exception as exc:  # noqa: BLE001 - startup must not fail
        _LOG.warn(f"account restore skipped: {exc}")
    return added
