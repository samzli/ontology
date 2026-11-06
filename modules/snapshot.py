"""Database snapshot sync for ephemeral (serverless) hosts.

Serverless filesystems are wiped on every cold start, so accounts registered
on one instance would vanish for the next. This module mirrors the SQLite
database to an external store and restores it on boot:

  * **Vercel Database Blob (new) — recommended** — no token needed on a
    connected project. The Vercel runtime injects ``BLOB_STORE_ID`` and the
    SDK authenticates via Vercel's built-in OIDC. Requires ``vercel-blob``
    in ``requirements.txt``.
  * **Vercel Blob REST API (legacy)** — set ``BLOB_READ_WRITE_TOKEN``; the
    module talks to ``https://blob.vercel-storage.com`` directly over httpx.
    Useful when the SDK is missing or an older Blob generation is used.
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
from typing import Any, Dict, List, Optional

import httpx

from common.logger import AppLogger
from modules import config as cfg

_LOG = AppLogger("snapshot")

_SNAP = cfg.SNAPSHOT
_TIMEOUT = float(_SNAP.get("timeout_s", 20))
_MIN_INTERVAL_S = float(_SNAP.get("min_interval_s", 5))
_BLOB_API = str(_SNAP.get("blob_api_base", "https://blob.vercel-storage.com"))
_BLOB_API_VERSION = str(_SNAP.get("blob_api_version", "7"))
_PREFIX = str(_SNAP.get("prefix", "onti")).strip("/")
_BLOB_TOKEN_ENV = str(_SNAP.get("blob_token_env", "BLOB_READ_WRITE_TOKEN"))
_BLOB_STORE_ID_ENV = str(_SNAP.get("blob_store_id_env", "BLOB_STORE_ID"))
_HTTP_URL_ENV = str(_SNAP.get("http_url_env", "ONTI_SNAPSHOT_URL"))
_HTTP_TOKEN_ENV = str(_SNAP.get("http_token_env", "ONTI_SNAPSHOT_TOKEN"))

_last_sync = 0.0


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _try_sdk():
    """Return ``(svc, sync_module)`` for the Vercel SDK, or ``None, None``.

    The SDK is only tried when ``BLOB_STORE_ID`` is set AND the ``vercel-blob``
    package is importable. Import errors degrade silently so the legacy REST
    path still works on hosts that intentionally skip the SDK.
    """
    store_id = _env(_BLOB_STORE_ID_ENV)
    if not store_id:
        return None, None
    try:
        from vercel.blob import sync as _vsync

        session = _vsync.get_active_sync_session()
        svc = _vsync.get_sync_blob_service(session)
        return svc, _vsync
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"vercel-blob SDK unavailable, falling back: {exc}")
        return None, None


def backend() -> str:
    """Name the configured snapshot backend.

    Returns:
        str: ``"blob_sdk"`` (Vercel Database Blob + SDK), ``"blob"`` (legacy
        REST API), ``"http"`` or ``""`` (sync disabled).
    """
    if _env(_BLOB_STORE_ID_ENV):
        svc, _ = _try_sdk()
        if svc is not None:
            return "blob_sdk"
    if _env(_BLOB_TOKEN_ENV):
        return "blob"
    if _env(_HTTP_URL_ENV):
        return "http"
    return ""


def enabled() -> bool:
    return backend() != ""


def _namespace() -> str:
    from modules import db

    return hmac.new(db.session_signing_key(), b"snapshot-path", "sha256").hexdigest()[
        :24
    ]


def _db_bytes() -> bytes:
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
    return data[:16] == b"SQLite format 3\x00"


# ---------------------------------------------------------------------------
# Legacy REST API helpers (bearer token + blob.vercel-storage.com)
# ---------------------------------------------------------------------------


def _blob_headers() -> dict:
    return {
        "authorization": f"Bearer {_env(_BLOB_TOKEN_ENV)}",
        "x-api-version": _BLOB_API_VERSION,
        "x-vercel-blob-access": "public",
    }


def _upload_blob(data: bytes) -> bool:
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


# ---------------------------------------------------------------------------
# Vercel SDK helpers (Database Blob + OIDC — no token to manage)
# ---------------------------------------------------------------------------


def _sdk_upload(data: bytes) -> bool:
    svc, _ = _try_sdk()
    if svc is None:
        return False
    path = f"{_PREFIX}/{_namespace()}/onti.db"
    try:
        svc.put(path, data, access="public")
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"sdk blob upload failed: {exc}")
        return False


def _sdk_download() -> Optional[bytes]:
    svc, _ = _try_sdk()
    if svc is None:
        return None
    prefix = f"{_PREFIX}/{_namespace()}/"
    try:
        page = svc.query_blobs_page(prefix=prefix, limit=10)
        items = list(page.items) if hasattr(page, "items") else list(page)
        if not items:
            return None
        latest = max(items, key=lambda b: getattr(b, "uploaded_at", None) or "")
        got = svc.open_download(latest.pathname)
        return got.read() if got else None
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"sdk blob download failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# HTTP operator endpoint helpers (unchanged)
# ---------------------------------------------------------------------------


def _upload_http(data: bytes) -> bool:
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
    resp = httpx.get(
        _env(_HTTP_URL_ENV),
        headers={"authorization": f"Bearer {_env(_HTTP_TOKEN_ENV)}"},
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 300:
        return None
    return resp.content


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_now() -> bool:
    kind = backend()
    if not kind:
        return False
    try:
        data = _db_bytes()
        if kind == "blob_sdk":
            ok = _sdk_upload(data)
        elif kind == "blob":
            ok = _upload_blob(data)
        else:
            ok = _upload_http(data)
        if ok:
            global _last_sync
            _last_sync = time.time()
            _LOG.info(f"snapshot uploaded via {kind} ({len(data)} bytes)")
        return ok
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"snapshot upload skipped: {exc}")
        return False


def request_sync() -> bool:
    if not enabled():
        return False
    if time.time() - _last_sync < _MIN_INTERVAL_S:
        return False
    return sync_now()


def restore_latest(target: Path) -> bool:
    kind = backend()
    if not kind:
        return False
    try:
        if kind == "blob_sdk":
            data = _sdk_download()
        elif kind == "blob":
            data = _download_blob()
        else:
            data = _download_http()
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
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"snapshot restore skipped: {exc}")
        return False


# ---------------------------------------------------------------------------
# Account records
# ---------------------------------------------------------------------------

_ACCOUNT_DIR = "shared/accounts"


def account_store() -> bool:
    return enabled()


def _account_key(username: str) -> str:
    return hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()


def _blob_account_path(username: str) -> str:
    return f"{_PREFIX}/{_ACCOUNT_DIR}/{_account_key(username)}.json"


def _http_account_url(username: str) -> str:
    base = _env(_HTTP_URL_ENV)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}account={_account_key(username)}"


def _put_account_sdk(username: str, payload: bytes) -> bool:
    svc, _ = _try_sdk()
    if svc is None:
        return False
    try:
        svc.put(_blob_account_path(username), payload, access="public")
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"sdk account upload failed: {exc}")
        return False


def _put_account_blob(username: str, payload: bytes) -> bool:
    path = _blob_account_path(username)
    resp = httpx.put(
        f"{_BLOB_API}/{path}",
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


def _get_account_sdk(username: str) -> Optional[bytes]:
    svc, _ = _try_sdk()
    if svc is None:
        return None
    path = _blob_account_path(username)
    try:
        got = svc.open_download(path)
        return bytes(got) if got else None
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"sdk account download failed: {exc}")
        return None


def _get_account_blob(username: str) -> Optional[bytes]:
    path = _blob_account_path(username)
    resp = httpx.get(
        f"{_BLOB_API}?prefix={path}&limit=1",
        headers=_blob_headers(),
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 300:
        return None
    for blob in resp.json().get("blobs", []):
        if blob.get("pathname") != path:
            continue
        got = httpx.get(blob["url"], timeout=_TIMEOUT)
        if got.status_code >= 300:
            return None
        return got.content
    return None


def save_account(record: Dict[str, Any]) -> bool:
    kind = backend()
    if not kind:
        return False
    username = str(record.get("username", "")).strip()
    if not username:
        return False
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
    try:
        if kind == "blob_sdk":
            return _put_account_sdk(username, payload)
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
        return resp.status_code < 300
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"account upload skipped: {exc}")
        return False


def load_account(username: str) -> Optional[Dict[str, Any]]:
    kind = backend()
    if not kind:
        return None
    try:
        if kind == "blob_sdk":
            raw = _get_account_sdk(username)
        elif kind == "blob":
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
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"account lookup skipped: {exc}")
        return None


def delete_account(username: str) -> bool:
    kind = backend()
    if not kind:
        return False
    try:
        if kind in ("blob_sdk", "blob"):
            path = _blob_account_path(username)
            if kind == "blob_sdk":
                svc, _ = _try_sdk()
                if svc is None:
                    return False
                svc.delete(path)
            else:
                resp = httpx.post(
                    f"{_BLOB_API}/delete",
                    json={"urls": [f"{_BLOB_API}/{path}"]},
                    headers=_blob_headers(),
                    timeout=_TIMEOUT,
                )
                if resp.status_code >= 300:
                    return False
            return True
        resp = httpx.delete(
            _http_account_url(username),
            headers={"authorization": f"Bearer {_env(_HTTP_TOKEN_ENV)}"},
            timeout=_TIMEOUT,
        )
        return resp.status_code < 300
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"account delete skipped: {exc}")
        return False


def persist_account(conn: sqlite3.Connection, username: str) -> bool:
    if not account_store():
        return True
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row is None:
        return False
    keys = set(row.keys())
    history: List[Dict[str, str]] = []
    try:
        history = [
            {"password_hash": h["password_hash"], "changed_at": h["changed_at"]}
            for h in conn.execute(
                "SELECT password_hash, changed_at FROM password_history "
                "WHERE user_id=? ORDER BY id DESC LIMIT 24",
                (row["id"],),
            ).fetchall()
        ]
    except sqlite3.Error:
        history = []
    return save_account(
        {
            "username": row["username"],
            "display_name": row["display_name"],
            "password_hash": row["password_hash"],
            "role": row["role"],
            "enabled": int(row["enabled"]),
            "created_at": row["created_at"],
            "email": row["email"] if "email" in keys else "",
            "email_verified": int(row["email_verified"])
            if "email_verified" in keys
            else 0,
            "password_changed_at": row["password_changed_at"]
            if "password_changed_at" in keys
            else "",
            "password_history": history,
        }
    )


def adopt_account(conn: sqlite3.Connection, record: Dict[str, Any]) -> bool:
    username = str(record.get("username", "")).strip()
    role = record.get("role") if record.get("role") in ("admin", "user") else "user"
    if not username:
        return False
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    email = str(record.get("email") or "").strip().lower()
    email_verified = int(record.get("email_verified") or 0)
    changed_at = str(record.get("password_changed_at") or "")
    conn.execute(
        "INSERT INTO users(username, display_name, password_hash, role, enabled, "
        "created_at, email, email_verified, password_changed_at) "
        "SELECT ?,?,?,?,?,?,?,?,? "
        "WHERE NOT EXISTS (SELECT 1 FROM users WHERE username=?)",
        (
            username,
            record.get("display_name") or username,
            record.get("password_hash") or "",
            role,
            int(record.get("enabled", 1)),
            record.get("created_at") or "",
            email if "email" in cols else "",
            email_verified if "email_verified" in cols else 0,
            changed_at if "password_changed_at" in cols else "",
            username,
        ),
    )
    row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row is not None and "password_changed_at" in cols:
        conn.execute(
            "UPDATE users SET email=?, email_verified=?, password_changed_at=? "
            "WHERE id=? AND email=''",
            (email, email_verified, changed_at, row["id"]),
        )
        history = record.get("password_history") or []
        if isinstance(history, list) and history:
            have = {
                r["password_hash"]
                for r in conn.execute(
                    "SELECT password_hash FROM password_history WHERE user_id=?",
                    (row["id"],),
                ).fetchall()
            }
            for item in history:
                if not isinstance(item, dict):
                    continue
                ph = str(item.get("password_hash") or "")
                if not ph or ph in have:
                    continue
                conn.execute(
                    "INSERT INTO password_history(user_id, password_hash, changed_at) "
                    "VALUES(?,?,?)",
                    (row["id"], ph, str(item.get("changed_at") or "")),
                )
                have.add(ph)
    return row is not None


def restore_accounts(conn: sqlite3.Connection) -> int:
    kind = backend()
    if kind not in ("blob_sdk", "blob"):
        return 0
    prefix = f"{_PREFIX}/{_ACCOUNT_DIR}/"
    added = 0
    try:
        if kind == "blob_sdk":
            svc, _ = _try_sdk()
            if svc is None:
                return 0
            blobs = list(svc.query_blobs(prefix=prefix, limit=1000))
            for blob in blobs:
                got = svc.open_download(blob.pathname)
                if not got:
                    continue
                try:
                    record = json.loads(bytes(got).decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(record, dict) and adopt_account(conn, record):
                    added += 1
        else:
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
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"account restore skipped: {exc}")
    return added


# ---------------------------------------------------------------------------
# E-mail verification challenges
# ---------------------------------------------------------------------------

_OTP_DIR = "shared/otp"


def otp_store() -> bool:
    return enabled()


def _otp_key(challenge_id: str) -> str:
    return hashlib.sha256(challenge_id.strip().encode("utf-8")).hexdigest()


def _blob_otp_path(challenge_id: str) -> str:
    return f"{_PREFIX}/{_OTP_DIR}/{_otp_key(challenge_id)}.json"


def _http_otp_url(challenge_id: str) -> str:
    base = _env(_HTTP_URL_ENV)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}otp={_otp_key(challenge_id)}"


def save_otp(challenge_id: str, record: Dict[str, Any]) -> bool:
    kind = backend()
    if not kind:
        return True
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
    try:
        if kind == "blob_sdk":
            svc, _ = _try_sdk()
            if svc is None:
                return False
            svc.put(_blob_otp_path(challenge_id), payload, access="public")
        elif kind == "blob":
            resp = httpx.put(
                f"{_BLOB_API}/{_blob_otp_path(challenge_id)}",
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
                _LOG.warn(f"otp upload failed: {resp.status_code} {resp.text[:200]}")
                return False
        else:
            resp = httpx.put(
                _http_otp_url(challenge_id),
                content=payload,
                headers={
                    "authorization": f"Bearer {_env(_HTTP_TOKEN_ENV)}",
                    "content-type": "application/json",
                },
                timeout=_TIMEOUT,
            )
            if resp.status_code >= 300:
                _LOG.warn(f"otp upload failed: {resp.status_code} {resp.text[:200]}")
                return False
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"otp upload skipped: {exc}")
        return False


def load_otp(challenge_id: str) -> Optional[Dict[str, Any]]:
    kind = backend()
    if not kind:
        return None
    try:
        if kind == "blob_sdk":
            svc, _ = _try_sdk()
            if svc is None:
                return None
            got = svc.open_download(_blob_otp_path(challenge_id))
            raw = bytes(got) if got else None
        elif kind == "blob":
            path = _blob_otp_path(challenge_id)
            listing = httpx.get(
                f"{_BLOB_API}?prefix={path}&limit=1",
                headers=_blob_headers(),
                timeout=_TIMEOUT,
            )
            if listing.status_code >= 300:
                return None
            blobs = listing.json().get("blobs", [])
            if not blobs:
                return None
            got = httpx.get(blobs[0]["url"], timeout=_TIMEOUT)
            raw = got.content if got.status_code < 300 else None
        else:
            resp = httpx.get(
                _http_otp_url(challenge_id),
                headers={"authorization": f"Bearer {_env(_HTTP_TOKEN_ENV)}"},
                timeout=_TIMEOUT,
            )
            raw = resp.content if resp.status_code < 300 else None
        if not raw:
            return None
        record = json.loads(raw.decode("utf-8"))
        return record if isinstance(record, dict) else None
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"otp lookup skipped: {exc}")
        return None


def delete_otp(challenge_id: str) -> bool:
    kind = backend()
    if not kind:
        return True
    try:
        if kind in ("blob_sdk", "blob"):
            path = _blob_otp_path(challenge_id)
            if kind == "blob_sdk":
                svc, _ = _try_sdk()
                if svc is None:
                    return False
                svc.delete(path)
            else:
                resp = httpx.post(
                    f"{_BLOB_API}/delete",
                    json={"urls": [f"{_BLOB_API}/{path}"]},
                    headers=_blob_headers(),
                    timeout=_TIMEOUT,
                )
                if resp.status_code >= 300:
                    return False
            return True
        resp = httpx.delete(
            _http_otp_url(challenge_id),
            headers={"authorization": f"Bearer {_env(_HTTP_TOKEN_ENV)}"},
            timeout=_TIMEOUT,
        )
        return resp.status_code < 300
    except Exception as exc:  # noqa: BLE001
        _LOG.warn(f"otp delete skipped: {exc}")
        return False
