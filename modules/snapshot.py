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

import hmac
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import httpx

from common.logger import AppLogger

_LOG = AppLogger("snapshot")

_TIMEOUT = 20.0
# Collapse bursts of account changes (e.g. an admin creating several users)
# into a single upload.
_MIN_INTERVAL_S = 5.0
_last_sync = 0.0

_BLOB_API = "https://blob.vercel-storage.com"
_BLOB_API_VERSION = "7"


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
    if _env("BLOB_READ_WRITE_TOKEN"):
        return "blob"
    if _env("ONTI_SNAPSHOT_URL"):
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

    A WAL checkpoint runs first so the main ``.db`` file alone carries every
    committed transaction and no sidecar needs uploading.

    Returns:
        bytes: Raw SQLite file contents.

    Raises:
        sqlite3.Error: When the database cannot be checkpointed or read.
    """
    from modules import db

    conn = sqlite3.connect(str(db.DB_PATH), timeout=30)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return Path(db.DB_PATH).read_bytes()


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
        "authorization": f"Bearer {_env('BLOB_READ_WRITE_TOKEN')}",
        "x-api-version": _BLOB_API_VERSION,
    }


def _upload_blob(data: bytes) -> bool:
    """PUT the snapshot to Vercel Blob under the secret prefix.

    Args:
        data (bytes): Database contents.

    Returns:
        bool: ``True`` on HTTP 2xx.
    """
    path = f"onti/{_namespace()}/onti.db"
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
    prefix = f"onti/{_namespace()}/"
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
        _env("ONTI_SNAPSHOT_URL"),
        content=data,
        headers={"authorization": f"Bearer {_env('ONTI_SNAPSHOT_TOKEN')}"},
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
        _env("ONTI_SNAPSHOT_URL"),
        headers={"authorization": f"Bearer {_env('ONTI_SNAPSHOT_TOKEN')}"},
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
