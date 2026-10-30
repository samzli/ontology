"""SQLite persistence layer for the Onti insurance ontology platform.

Single-file database at ``data/onti.db`` (``ONTI_DATA_DIR`` / ``/tmp/onti-data``
on read-only hosts) using only the Python standard library. This module owns:

  * schema creation / migration (``init_db``)
  * a small connection helper (``get_conn``)
  * key/value application settings (``get_setting`` / ``set_setting``)
  * the audit-log helper (``audit``)
  * machine-local symmetric encryption for model API keys
    (HMAC-SHA256 counter stream + encrypt-then-MAC, no third-party deps)

Domain data (packs, entities, datasets, ...) is managed by the service
modules; here we only define the structure and generic helpers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
BUNDLED_DATA_DIR = BASE_DIR / "data"


def _resolve_data_dir() -> Path:
    """Return the writable data directory for the current host.

    Local runs keep everything in ``data/`` (next to the code). Serverless
    hosts such as Vercel mount the deployment read-only, so SQLite (and the
    ``-wal`` sidecar it creates) can only live on the writable ``/tmp``
    volume. ``ONTI_DATA_DIR`` overrides both defaults.

    Returns:
        Path: Resolved, absolute data directory.

    Raises:
        OSError: When the override path cannot be resolved.

    Example:
        >>> _resolve_data_dir().name
        'data'
    """
    override = os.environ.get("ONTI_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.environ.get("VERCEL") and os.name != "nt":
        return Path("/tmp/onti-data")
    return BUNDLED_DATA_DIR


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "onti.db"
# The machine key stays next to the bundled data when available so encrypted
# model API keys remain decryptable across instances (``/tmp`` is per-instance).
KEY_PATH = BUNDLED_DATA_DIR / ".onti.key"
_BUNDLED_DB_PATH = BUNDLED_DATA_DIR / "onti.db"
_READY = False


def _prepare_data_dir() -> None:
    """Create the writable data directory and seed it from the bundle once.

    Copies the bundled ``onti.db`` (dropping its ``-wal``/``-shm`` sidecars)
    when the runtime database does not exist yet. The work is guarded by the
    module-level ``_READY`` flag, so it happens at most once per process.

    Returns:
        None: The directory and database are ready after this call.

    Raises:
        OSError: When the directory cannot be created or the database cannot
            be copied.

    Example:
        >>> _prepare_data_dir()
    """
    global _READY
    if _READY:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if (
        DB_PATH != _BUNDLED_DB_PATH
        and not DB_PATH.exists()
        and _BUNDLED_DB_PATH.exists()
    ):
        # Ship the pre-seeded database with the deployment so every cold start
        # exposes the same packs, accounts and reports.
        shutil.copy2(_BUNDLED_DB_PATH, DB_PATH)
        for suffix in ("-wal", "-shm"):
            side = Path(str(DB_PATH) + suffix)
            if side.exists():
                side.unlink()
    _READY = True


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection with Row factory and foreign keys enabled.

    The connection is committed on success, rolled back on exception, and
    always closed.

    Yields:
        sqlite3.Connection: Connection valid for the ``with`` block.

    Raises:
        sqlite3.Error: When the database cannot be opened or a statement
            fails.

    Example:
        >>> with get_conn() as conn:
        ...     conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        1
    """
    _prepare_data_dir()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now() -> str:
    """Return the current UTC timestamp.

    Returns:
        str: ISO-8601 text with second precision and a ``+00:00`` offset.

    Example:
        >>> now()[:4]
        '2026'
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# JSON helpers (used by row mappers)
# ---------------------------------------------------------------------------


def jloads(value: Optional[str], default: Any) -> Any:
    """Decode a JSON text column, falling back to a default value.

    Args:
        value (Optional[str]): Raw JSON text; ``None`` or ``""`` yield the
            default.
        default (Any): Value returned when decoding is impossible.

    Returns:
        Any: Decoded object, or ``default`` when the text is missing or
            malformed.

    Example:
        >>> jloads("[1, 2]", [])
        [1, 2]
    """
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def jdumps(value: Any) -> str:
    """Encode a value as JSON, keeping non-ASCII characters readable.

    Args:
        value (Any): Value to encode.

    Returns:
        str: JSON text produced with ``ensure_ascii=False``.

    Example:
        >>> jdumps({"k": "v"})
        '{"k": "v"}'
    """
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


DEFAULT_SETTINGS: Dict[str, str] = {
    "active_pack_id": "",
    "allow_registration": "1",
    "upload_max_mb": "10",
    # After a data upload is committed, ask the configured LLM to propose
    # ontology additions (entities / relations / nodes / edges / formulas).
    "ai_auto_propose": "1",
}


def get_setting(
    conn: sqlite3.Connection, key: str, default: Optional[str] = None
) -> Optional[str]:
    """Read an application setting, falling back to defaults.

    Args:
        conn (sqlite3.Connection): Open database connection.
        key (str): Setting key.
        default (Optional[str]): Fallback used when neither the table nor
            ``DEFAULT_SETTINGS`` holds the key.

    Returns:
        Optional[str]: Stored value, built-in default, or ``default``.

    Example:
        >>> get_setting(conn, "allow_registration")
        '1'
    """
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Insert or update an application setting.

    Args:
        conn (sqlite3.Connection): Open database connection.
        key (str): Setting key.
        value (Any): Value, stored after ``str`` conversion.

    Returns:
        None: The change is committed by the caller.

    Example:
        >>> set_setting(conn, "upload_max_mb", 10)
    """
    conn.execute(
        "INSERT INTO app_settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def active_pack_id(conn: sqlite3.Connection) -> Optional[int]:
    """Return the active pack id stored in the settings table.

    Args:
        conn (sqlite3.Connection): Open database connection.

    Returns:
        Optional[int]: Parsed pack id, or ``None`` when unset or malformed.

    Example:
        >>> active_pack_id(conn)
        1
    """
    raw = get_setting(conn, "active_pack_id", "")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def audit(
    conn: sqlite3.Connection,
    username: str,
    action: str,
    detail: str = "",
    ip: str = "",
) -> None:
    """Append an entry to the audit log.

    Args:
        conn (sqlite3.Connection): Open database connection.
        username (str): Actor name (``"system"`` for automated events).
        action (str): Machine-readable action key, e.g. ``"pack.create"``.
        detail (str): Free-form detail, truncated to 1000 characters.
        ip (str): Client IP address.

    Returns:
        None: The entry is committed by the caller.

    Example:
        >>> audit(conn, "admin", "pack.create", "id=7 slug=demo")
    """
    conn.execute(
        "INSERT INTO audit_log(ts, username, action, detail, ip) VALUES(?,?,?,?,?)",
        (now(), username, action, detail[:1000], ip),
    )


# ---------------------------------------------------------------------------
# Machine-local secret + symmetric encryption for API keys
# ---------------------------------------------------------------------------


def _read_master_key(path: Path) -> Optional[bytes]:
    """Read and decode a base64-encoded 32-byte master key file.

    Args:
        path (Path): Candidate key file.

    Returns:
        Optional[bytes]: The key bytes, or ``None`` when the file is missing,
            corrupt, or not exactly 32 bytes.

    Example:
        >>> _read_master_key(KEY_PATH) is None
        False
    """
    if not path.exists():
        return None
    raw = path.read_bytes().strip()
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception:  # noqa: BLE001 - corrupt key file -> fall through
        return None
    return key if len(key) == 32 else None


def _machine_key() -> bytes:
    """Load, or create, the 32-byte machine-local master key.

    The key is looked up in ``KEY_PATH`` first and then in the writable data
    directory; when neither exists a fresh key is generated and written with
    owner-only permissions (best effort).

    Returns:
        bytes: The 32-byte master key.

    Raises:
        OSError: When a new key cannot be written to the data directory.

    Example:
        >>> len(_machine_key())
        32
    """
    for candidate in (KEY_PATH, DATA_DIR / ".onti.key"):
        key = _read_master_key(candidate)
        if key is not None:
            return key
    key = secrets.token_bytes(32)
    _prepare_data_dir()
    key_file = DATA_DIR / ".onti.key"
    key_file.write_bytes(base64.urlsafe_b64encode(key))
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


def session_signing_key() -> bytes:
    """Key used to sign stateless session tokens (HMAC-SHA256).

    Derived from the same machine key, so every instance of the deployment
    validates the same cookies even though the SQLite session table on
    ``/tmp`` is per-instance.

    Returns:
        bytes: 32-byte key derived from the master key.

    Example:
        >>> len(session_signing_key())
        32
    """
    return hashlib.sha256(_machine_key() + b"session").digest()


def _keystream(mac_key: bytes, nonce: bytes, length: int) -> bytes:
    """Generate a deterministic HMAC-SHA256 keystream.

    Args:
        mac_key (bytes): Key for the HMAC blocks.
        nonce (bytes): Per-message nonce mixed into every block.
        length (int): Number of keystream bytes required.

    Returns:
        bytes: Exactly ``length`` keystream bytes.

    Example:
        >>> len(_keystream(b"k", b"n", 16))
        16
    """
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            mac_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret with machine-local keys (encrypt-then-MAC).

    Args:
        plaintext (str): Secret to encrypt; ``None`` or ``""`` yield ``""``.

    Returns:
        str: URL-safe base64 blob of ``nonce || tag || ciphertext``.

    Raises:
        OSError: When the machine key cannot be created.

    Example:
        >>> decrypt_secret(encrypt_secret("sk-123456"))
        'sk-123456'
    """
    if plaintext is None or plaintext == "":
        return ""
    master = _machine_key()
    enc_key = hashlib.sha256(master + b"enc").digest()
    mac_key = hashlib.sha256(master + b"mac").digest()
    nonce = secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    stream = _keystream(enc_key, nonce, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")


def decrypt_secret(blob: str) -> str:
    """Decrypt a blob produced by :func:`encrypt_secret`.

    Args:
        blob (str): URL-safe base64 blob; empty input returns ``""``.

    Returns:
        str: The plaintext, or ``""`` when the blob is corrupt, truncated or
            fails the authentication check.

    Example:
        >>> decrypt_secret(encrypt_secret("sk-123456"))
        'sk-123456'
    """
    if not blob:
        return ""
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        nonce, tag, cipher = raw[:16], raw[16:48], raw[48:]
        master = _machine_key()
        enc_key = hashlib.sha256(master + b"enc").digest()
        mac_key = hashlib.sha256(master + b"mac").digest()
        expected = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            return ""
        stream = _keystream(enc_key, nonce, len(cipher))
        return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8")
    except Exception:  # noqa: BLE001 - never crash on a bad/old secret
        return ""


def mask_secret(plaintext: str) -> str:
    """Mask a secret for display, keeping the first and last four characters.

    Args:
        plaintext (str): Secret to mask.

    Returns:
        str: ``""`` for empty input, ``"****"`` for secrets of 8 characters or
            fewer, otherwise ``first4****last4``.

    Example:
        >>> mask_secret("sk-123456789")
        'sk-1****6789'
    """
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "****"
    return plaintext[:4] + "****" + plaintext[-4:]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',           -- admin | user
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login    TEXT,
    login_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT ''
);

-- Tokens revoked by logout. Kept so a stateless (serverless) instance can no
-- longer honour a token whose ``sessions`` row lived on another instance.
CREATE TABLE IF NOT EXISTS revoked_sessions (
    token      TEXT PRIMARY KEY,
    revoked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ontology_packs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    slug           TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL DEFAULT 'onti-pack/v1',
    narrative      TEXT NOT NULL DEFAULT '',
    origin         TEXT NOT NULL DEFAULT '',
    version        INTEGER NOT NULL DEFAULT 1,
    is_builtin     INTEGER NOT NULL DEFAULT 0,
    is_active      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id     INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    label_en    TEXT NOT NULL,
    layer       INTEGER NOT NULL DEFAULT 3,
    description TEXT NOT NULL DEFAULT '',
    sort        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pack_id, key)
);

CREATE TABLE IF NOT EXISTS attributes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id     INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    entity_key  TEXT NOT NULL,
    key         TEXT NOT NULL,
    label_en    TEXT NOT NULL,
    data_type   TEXT NOT NULL DEFAULT 'STRING',   -- STRING | INTEGER | FLOAT
    unit        TEXT NOT NULL DEFAULT '',
    enum_values TEXT NOT NULL DEFAULT '',
    required    INTEGER NOT NULL DEFAULT 0,
    is_unique   INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    sort        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pack_id, entity_key, key)
);

CREATE TABLE IF NOT EXISTS relations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id       INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    label_en      TEXT NOT NULL DEFAULT '',
    cardinality   TEXT NOT NULL DEFAULT 'MANY_TO_MANY',
    source_key    TEXT NOT NULL,
    target_key    TEXT NOT NULL,
    source_attr   TEXT NOT NULL DEFAULT '',
    target_attr   TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    sort          INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pack_id, key)
);

CREATE TABLE IF NOT EXISTS graph_nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id     INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    grp         TEXT NOT NULL DEFAULT 'financial',
    value       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    aliases     TEXT NOT NULL DEFAULT '[]',
    is_core     INTEGER NOT NULL DEFAULT 0,
    sort        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pack_id, key)
);

CREATE TABLE IF NOT EXISTS graph_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id     INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    source_key  TEXT NOT NULL,           -- comma-separated source node keys (multi-input edge)
    target_key  TEXT NOT NULL,
    edge_type   TEXT NOT NULL DEFAULT 'STRUCTURAL',
    label       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    sort        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pack_id, key)
);

CREATE TABLE IF NOT EXISTS metric_formulas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id    INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    expression TEXT NOT NULL,
    unit       TEXT NOT NULL DEFAULT '',
    fmt        TEXT NOT NULL DEFAULT 'number',  -- number | currency_m | percent
    decimals   INTEGER NOT NULL DEFAULT 2,
    sort       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pack_id, key)
);

CREATE TABLE IF NOT EXISTS question_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id        INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    key            TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT '',
    patterns       TEXT NOT NULL DEFAULT '[]',   -- OR-group of regexes
    require_groups TEXT NOT NULL DEFAULT '[]',   -- AND-groups, each a list of regexes
    guards         TEXT NOT NULL DEFAULT '[]',   -- skip if any matches
    template       TEXT NOT NULL,
    evidence_nodes TEXT NOT NULL DEFAULT '[]',
    path_steps     TEXT NOT NULL DEFAULT '[]',
    sort           INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pack_id, key)
);

CREATE TABLE IF NOT EXISTS pack_tables (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id            INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    key                TEXT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    entity_key         TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    active_dataset_id  INTEGER,
    UNIQUE(pack_id, key)
);

CREATE TABLE IF NOT EXISTS pack_columns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id    INTEGER NOT NULL REFERENCES pack_tables(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    data_type   TEXT NOT NULL DEFAULT 'STRING',
    unit        TEXT NOT NULL DEFAULT '',
    enum_values TEXT NOT NULL DEFAULT '',
    expression  TEXT NOT NULL DEFAULT '',     -- non-empty => derived column
    fmt         TEXT NOT NULL DEFAULT 'raw',  -- raw | number | currency_m | currency_k | percent
    decimals    INTEGER NOT NULL DEFAULT 2,
    required    INTEGER NOT NULL DEFAULT 0,
    sort        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(table_id, key)
);

CREATE TABLE IF NOT EXISTS datasets (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id   INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    table_id  INTEGER NOT NULL REFERENCES pack_tables(id) ON DELETE CASCADE,
    name      TEXT NOT NULL DEFAULT '',
    filename  TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    kind      TEXT NOT NULL DEFAULT 'upload',  -- seed | upload
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pack_rows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL DEFAULT 0,
    data       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_pack_rows_dataset ON pack_rows(dataset_id);

CREATE TABLE IF NOT EXISTS question_bank (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id    INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    question   TEXT NOT NULL,
    norm       TEXT NOT NULL,
    category   TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'imported',  -- canonical | imported
    created_at TEXT NOT NULL,
    UNIQUE(pack_id, norm)
);

CREATE TABLE IF NOT EXISTS model_configs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    base_url    TEXT NOT NULL,
    api_key_enc TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL,
    temperature REAL NOT NULL DEFAULT 0.3,
    max_tokens  INTEGER NOT NULL DEFAULT 1024,
    timeout     REAL NOT NULL DEFAULT 90,
    verify_ssl  INTEGER NOT NULL DEFAULT 1,
    enabled     INTEGER NOT NULL DEFAULT 1,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        TEXT NOT NULL UNIQUE,
    owner_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    pack_id    INTEGER,
    title      TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'session',
    sections   TEXT NOT NULL DEFAULT '[]',
    include_ai INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'ready',   -- ready | failed
    qa_count   INTEGER NOT NULL DEFAULT 0,
    md_path    TEXT NOT NULL DEFAULT '',
    html_path  TEXT NOT NULL DEFAULT '',
    pdf_path   TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    action   TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT '',
    ip       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    user_id         INTEGER,
    model_config_id INTEGER,
    model           TEXT NOT NULL DEFAULT '',
    success         INTEGER NOT NULL DEFAULT 1,
    latency_ms      REAL NOT NULL DEFAULT 0,
    tokens          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ontology_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    pack_id     INTEGER NOT NULL,
    table_id    INTEGER,
    dataset_id  INTEGER,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL,
    rationale   TEXT NOT NULL DEFAULT '',
    evidence    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    decided_by  TEXT NOT NULL DEFAULT '',
    decided_at  TEXT,
    result      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON ontology_proposals(status, pack_id);

CREATE TABLE IF NOT EXISTS pack_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id    INTEGER NOT NULL REFERENCES ontology_packs(id) ON DELETE CASCADE,
    label      TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    counts     TEXT NOT NULL DEFAULT '{}',   -- JSON summary: entities/relations/... totals
    payload    TEXT NOT NULL                  -- full onti-pack/v1 JSON snapshot
);
CREATE INDEX IF NOT EXISTS idx_pack_versions ON pack_versions(pack_id, id DESC);

CREATE TABLE IF NOT EXISTS user_questions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    pack_id    INTEGER,
    question   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_questions ON user_questions(user_id, id DESC);

CREATE TABLE IF NOT EXISTS ask_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    pack_id    INTEGER,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'rules',   -- rules | ai
    matched    INTEGER NOT NULL DEFAULT 1,
    tokens     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ask_history_user
    ON ask_history(user_id, pack_id, id DESC);

CREATE TABLE IF NOT EXISTS user_model_configs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    api_key_enc TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL,
    temperature REAL NOT NULL DEFAULT 0.3,
    max_tokens  INTEGER NOT NULL DEFAULT 1024,
    timeout     REAL NOT NULL DEFAULT 90,
    verify_ssl  INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS studio_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    username  TEXT NOT NULL,
    pack_id   INTEGER,
    pack_name TEXT NOT NULL DEFAULT '',
    action    TEXT NOT NULL,              -- ontology | preview | data-upload |
                                        -- ontology-upload | report
    title     TEXT NOT NULL DEFAULT '',
    detail    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_studio_history_user
    ON studio_history(user_id, id DESC);
"""


def init_db(seed: bool = True) -> None:
    """Create all tables (idempotent) and run the first-run seed migration.

    Applies additive ``ALTER TABLE`` migrations for columns added after a
    database was first created, then seeds the default settings. When ``seed``
    is true the bundled ontology packs and the default administrator account
    are ensured as well.

    Args:
        seed (bool): Also run the domain seed and default-admin provisioning.
            Defaults to True.

    Returns:
        None: The schema is ready after this call.

    Raises:
        sqlite3.Error: When schema creation or migration fails.

    Example:
        >>> init_db(seed=False)
    """
    _prepare_data_dir()
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES(?, ?)",
                (key, value),
            )
        # --- additive migrations for existing databases ---
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "last_login" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
        if "login_count" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN login_count INTEGER NOT NULL DEFAULT 0"
            )
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(ontology_packs)").fetchall()
        }
        if "origin" not in cols:
            conn.execute(
                "ALTER TABLE ontology_packs ADD COLUMN origin TEXT NOT NULL DEFAULT ''"
            )
        if "version" not in cols:
            conn.execute(
                "ALTER TABLE ontology_packs ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
            )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ask_history)").fetchall()}
        if cols and "answer" not in cols:
            conn.execute(
                "ALTER TABLE ask_history ADD COLUMN answer TEXT NOT NULL DEFAULT ''"
            )
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(ontology_proposals)").fetchall()
        }
        if cols and "evidence" not in cols:
            conn.execute(
                "ALTER TABLE ontology_proposals "
                "ADD COLUMN evidence TEXT NOT NULL DEFAULT ''"
            )
        # Per-config TLS: self-signed gateways (e.g. D.Nova) must be able to
        # opt out of SSL verification without a code change. New installs
        # get the column from the _SCHEMA above; this block catches rows
        # created before the column existed.
        for table in ("model_configs", "user_model_configs"):
            cols = {
                r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if cols and "verify_ssl" not in cols:
                conn.execute(
                    f"ALTER TABLE {table} "
                    "ADD COLUMN verify_ssl INTEGER NOT NULL DEFAULT 1"
                )
    if seed:
        # Local import keeps db.py free of domain dependencies.
        from modules.seed import ensure_seed, migrate_legacy_model
        from modules.auth import seed_default_admin

        with get_conn() as conn:
            ensure_seed(conn)
            seed_default_admin(conn)
        migrate_legacy_model()


if __name__ == "__main__":
    init_db()
    print(f"[db] Initialized {DB_PATH}")
