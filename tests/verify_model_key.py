"""Model API-key handling: normalisation, persistence and error translation.

Regression cover for the client-reported failure: pasting a new key into the
admin "Edit model" dialog and testing/saving it returned the provider's raw
``{"code":30014,"data":null,"message":"Token is invalid."}`` even though the
key was valid, because copy-paste artefacts (quotes, a ``Bearer`` prefix, a
trailing newline/space) were stored and sent verbatim.

Offline test — no server and no live provider call required:

    python tests/verify_model_key.py

Covers:
  1. ``normalize_api_key`` strips quotes (straight/typographic/back-tick),
     ``Bearer`` prefixes, whitespace and rejects placeholder values
  2. ``normalize_base_url`` trims spaces and trailing slashes
  3. ``describe_provider_error`` maps 30014/401/404/429/5xx/unreachable to a
     human-readable cause + hint and preserves the raw body for support
  4. saving a key through ``create_config`` / ``update_config`` stores the
     clean value (never the quoted one), while ``__unchanged__`` keeps it
  5. ``resolve_test_config`` returns a normalised key for both the stored and
     the ad-hoc path, including a legacy row saved dirty
  6. ``ConfiguredLLM`` builds an Authorization header with no artefacts
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import llm_client as llm  # noqa: E402
from modules.db import decrypt_secret, encrypt_secret, get_conn, now  # noqa: E402

KEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
STAMP = now().replace(":", "").replace("-", "").replace(" ", "")[:14]
PREFIX = "keytest" + STAMP
PASS = 0
FAIL = 0
CREATED: list = []


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK  " + label)
    else:
        FAIL += 1
        print("  FAIL " + label + (("  :: " + str(detail)) if detail else ""))


def _check_env_migration() -> None:
    """Prove the .env bootstrap import stores the key without its quotes.

    ``SILICONFLOW_API_KEY='sk-…'`` is exactly how the deployed ``.env`` is
    written, and that is the row the admin console shows as *Default model
    (migrated from .env)*. The migration runs against a throwaway SQLite
    file, so the real database is untouched.
    """
    import contextlib
    import os
    import shutil
    import sqlite3
    import tempfile

    from modules import seed as seed_mod

    tmpdir = Path(tempfile.mkdtemp(prefix="onti_env_test_"))
    (tmpdir / ".env").write_text(
        "SILICONFLOW_API_KEY='\"" + KEY + "\"'\n"
        "SILICONFLOW_BASE_URL='  https://api.siliconflow.cn/v1/  '\n"
        "SILICONFLOW_MODEL_NAME=' deepseek-ai/DeepSeek-V4-Flash '\n",
        encoding="utf-8",
    )
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute(
        "CREATE TABLE model_configs(id INTEGER PRIMARY KEY, name TEXT, base_url TEXT,"
        " api_key_enc TEXT, model TEXT, temperature REAL, max_tokens INTEGER,"
        " timeout REAL, verify_ssl INTEGER NOT NULL DEFAULT 1,"
        " enabled INTEGER, is_default INTEGER, created_at TEXT)"
    )
    mem.execute(
        "CREATE TABLE audit_log(ts TEXT, username TEXT, action TEXT, detail TEXT,"
        " ip TEXT)"
    )

    @contextlib.contextmanager
    def fake_conn():
        yield mem

    old_base, old_conn = seed_mod.BASE_DIR, seed_mod.get_conn
    saved_env = {
        k: os.environ.pop(k)
        for k in list(os.environ)
        if k.startswith(("LLM_", "SILICONFLOW_"))
    }
    seed_mod.BASE_DIR, seed_mod.get_conn = tmpdir, fake_conn
    try:
        seed_mod.migrate_legacy_model()
    finally:
        seed_mod.BASE_DIR, seed_mod.get_conn = old_base, old_conn
        os.environ.update(saved_env)

    row = mem.execute("SELECT * FROM model_configs").fetchone()
    ok(row is not None, "env bootstrap created the default model")
    if row is not None:
        ok(
            decrypt_secret(row["api_key_enc"]) == KEY,
            "quoted .env key stored without quotes",
            repr(decrypt_secret(row["api_key_enc"])),
        )
        ok(
            row["base_url"] == "https://api.siliconflow.cn/v1",
            "quoted .env base URL stored clean",
            row["base_url"],
        )
        ok(
            row["model"] == "deepseek-ai/DeepSeek-V4-Flash",
            "quoted .env model id trimmed",
            row["model"],
        )

    # A placeholder in .env must not create a bogus default model at all.
    mem.execute("DELETE FROM model_configs")
    (tmpdir / ".env").write_text(
        "SILICONFLOW_API_KEY='sk-...'\nSILICONFLOW_MODEL_NAME='m'\n", encoding="utf-8"
    )
    seed_mod.BASE_DIR, seed_mod.get_conn = tmpdir, fake_conn
    try:
        seed_mod.migrate_legacy_model()
    finally:
        seed_mod.BASE_DIR, seed_mod.get_conn = old_base, old_conn
    ok(
        mem.execute("SELECT COUNT(*) AS n FROM model_configs").fetchone()["n"] == 0,
        "placeholder .env key migrates nothing",
    )
    mem.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    # ------------------------------------------------ 1. normalise the key
    print("[1] normalize_api_key")
    ok(llm.normalize_api_key(KEY) == KEY, "bare key untouched")
    ok(llm.normalize_api_key("  " + KEY + "  ") == KEY, "surrounding spaces removed")
    ok(llm.normalize_api_key(KEY + "\n") == KEY, "trailing newline removed")
    ok(llm.normalize_api_key(KEY + "\r\n") == KEY, "trailing CRLF removed")
    ok(llm.normalize_api_key('"' + KEY + '"') == KEY, "double quotes removed")
    ok(llm.normalize_api_key("'" + KEY + "'") == KEY, "single quotes removed")
    ok(llm.normalize_api_key("`" + KEY + "`") == KEY, "back-ticks removed")
    ok(
        llm.normalize_api_key("\u201c" + KEY + "\u201d") == KEY,
        "typographic quotes removed",
    )
    ok(llm.normalize_api_key("Bearer " + KEY) == KEY, "Bearer prefix removed")
    ok(llm.normalize_api_key("bearer " + KEY) == KEY, "lower-case bearer removed")
    ok(
        llm.normalize_api_key(" 'Bearer " + KEY + "' ") == KEY,
        "quotes + Bearer + spaces combined",
    )
    ok(llm.normalize_api_key(None) == "", "None -> empty")
    ok(llm.normalize_api_key("") == "", "empty stays empty")
    ok(llm.normalize_api_key("sk-...") == "", "placeholder sk-... rejected")
    ok(llm.normalize_api_key("your-api-key") == "", "placeholder rejected")
    ok(llm.normalize_api_key("k") == "k", "short real key preserved")

    print("[2] normalize_base_url")
    ok(
        llm.normalize_base_url("  https://api.siliconflow.cn/v1/  ")
        == "https://api.siliconflow.cn/v1",
        "spaces + trailing slash trimmed",
    )
    ok(llm.normalize_base_url(None) == "", "None -> empty")

    # ------------------------------------------------ 3. error translation
    print("[3] describe_provider_error")
    raw = '{"code":30014,"data":null,"message":"Token is invalid."}'
    d = llm.describe_provider_error(200, raw)
    ok("rejected the API key" in d["error"], "30014 -> key rejected", d["error"])
    ok("30014" in d["error"], "provider code surfaced", d["error"])
    ok("Token is invalid" in d["error"], "provider message surfaced", d["error"])
    ok(bool(d["hint"]) and "quotes" in d["hint"], "actionable hint about quoting")
    ok(d["provider_error"] == raw, "raw body preserved for support")
    ok(d["provider_code"] == 30014, "code parsed", d["provider_code"])
    ok(
        "raw JSON" not in d["error"] and '"code"' not in d["error"],
        "raw JSON is not the user-facing message",
        d["error"],
    )

    d401 = llm.describe_provider_error(401, "")
    ok("rejected the API key" in d401["error"], "401 -> key rejected", d401["error"])
    # Even when the provider sends an empty body (Nova 401), provider_error
    # must not be empty — support needs something to work with.
    ok(d401["provider_error"], "401 empty body -> provider_error synthesised")
    ok(
        "HTTP 401" in d401["provider_error"],
        "401 empty body -> provider_error carries status",
    )

    d404 = llm.describe_provider_error(404, '{"error":{"message":"model not found"}}')
    ok(
        "could not find this model" in d404["error"],
        "404 -> model missing",
        d404["error"],
    )

    d429 = llm.describe_provider_error(429, "")
    ok("rate-limited" in d429["error"], "429 -> rate limited", d429["error"])

    d500 = llm.describe_provider_error(503, "")
    ok("internal error" in d500["error"], "5xx -> provider failure", d500["error"])

    dnet = llm.describe_provider_error(None, "connection refused")
    ok(
        "could not be reached" in dnet["error"],
        "no status -> unreachable",
        dnet["error"],
    )

    dtext = llm.describe_provider_error(400, "not json at all")
    ok("HTTP 400" in dtext["error"], "non-JSON body still explained", dtext["error"])

    # ------------------------------------------------ 4/5. persistence path
    print("[4] create_config / update_config persist the clean key")
    with get_conn() as conn:
        cid = llm.create_config(
            conn,
            "verify_model_key",
            name=PREFIX + "-create",
            base_url="  https://api.siliconflow.cn/v1///  ",
            api_key='  "' + KEY + '"\n',
            model="deepseek-ai/DeepSeek-V4-Flash",
        )
        CREATED.append(cid)
        row = conn.execute(
            "SELECT base_url, api_key_enc FROM model_configs WHERE id=?", (cid,)
        ).fetchone()
        ok(decrypt_secret(row["api_key_enc"]) == KEY, "create stored the clean key")
        ok(
            row["base_url"] == "https://api.siliconflow.cn/v1",
            "create stored the clean base URL",
            row["base_url"],
        )

        llm.update_config(
            conn, "verify_model_key", cid, {"api_key": "  '" + KEY + "'  "}
        )
        again = conn.execute(
            "SELECT api_key_enc FROM model_configs WHERE id=?", (cid,)
        ).fetchone()
        ok(
            decrypt_secret(again["api_key_enc"]) == KEY,
            "update stored the clean key",
            decrypt_secret(again["api_key_enc"]),
        )

        llm.update_config(conn, "verify_model_key", cid, {"api_key": "__unchanged__"})
        kept = conn.execute(
            "SELECT api_key_enc FROM model_configs WHERE id=?", (cid,)
        ).fetchone()
        ok(
            decrypt_secret(kept["api_key_enc"]) == KEY,
            "__unchanged__ keeps the existing key",
        )

        llm.update_config(conn, "verify_model_key", cid, {"api_key": "sk-..."})
        cleared = conn.execute(
            "SELECT api_key_enc FROM model_configs WHERE id=?", (cid,)
        ).fetchone()
        ok(
            decrypt_secret(cleared["api_key_enc"]) == "",
            "placeholder is never persisted as a key",
        )
        llm.update_config(conn, "verify_model_key", cid, {"api_key": KEY})

        print("[5] resolve_test_config normalises every path")
        stored = llm.resolve_test_config(conn, config_id=cid)
        ok(stored["api_key"] == KEY, "stored path returns the bare key")

        adhoc = llm.resolve_test_config(
            conn,
            name="t",
            base_url=" https://api.siliconflow.cn/v1/ ",
            api_key=" Bearer '" + KEY + "' ",
            model=" deepseek-ai/DeepSeek-V4-Flash ",
        )
        ok(
            adhoc["api_key"] == KEY,
            "ad-hoc path returns the bare key",
            adhoc["api_key"],
        )
        ok(
            adhoc["base_url"] == "https://api.siliconflow.cn/v1",
            "ad-hoc base URL normalised",
            adhoc["base_url"],
        )
        ok(
            adhoc["model"] == "deepseek-ai/DeepSeek-V4-Flash",
            "ad-hoc model id trimmed",
            adhoc["model"],
        )

        # Legacy row written before normalisation existed.
        legacy_id = conn.execute(
            "INSERT INTO model_configs(name, base_url, api_key_enc, model, temperature,"
            " max_tokens, timeout, enabled, is_default, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,0,?)",
            (
                PREFIX + "-legacy",
                "https://api.siliconflow.cn/v1",
                encrypt_secret('"' + KEY + '"'),
                "deepseek-ai/DeepSeek-V4-Flash",
                0.3,
                1024,
                90,
                1,
                now(),
            ),
        ).lastrowid
        CREATED.append(legacy_id)
        legacy = llm.resolve_test_config(conn, config_id=legacy_id)
        ok(legacy["api_key"] == KEY, "legacy dirty row cleaned on the way out")

        print("[6] ConfiguredLLM builds a clean Authorization header")
        client = llm.ConfiguredLLM(
            {
                "name": "t",
                "base_url": " https://api.siliconflow.cn/v1/ ",
                "api_key": " 'Bearer " + KEY + "' ",
                "model": "deepseek-ai/DeepSeek-V4-Flash",
            }
        )
        auth = client._headers.get("Authorization", "")
        ok(auth == "Bearer " + KEY, "header carries only the bare key", auth)
        ok(
            client.base_url == "https://api.siliconflow.cn/v1",
            "client base URL normalised",
            client.base_url,
        )
        no_key = llm.ConfiguredLLM(
            {"name": "t", "base_url": "https://x/v1", "api_key": "sk-...", "model": "m"}
        )
        ok(
            "Authorization" not in no_key._headers,
            "placeholder key sends no Authorization header",
            dict(no_key._headers),
        )

    print("[7] Connection failures are classified by cause")
    # The reported bug: a self-signed gateway surfaced as "could not be
    # reached", sending operators after a network problem instead of the
    # Verify SSL toggle.
    cert = ssl.SSLCertVerificationError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed "
        "certificate in certificate chain (_ssl.c:1144)"
    )
    wrapped = httpx.ConnectError(str(cert))
    wrapped.__cause__ = cert

    dt = llm.describe_transport_error(wrapped)
    ok(
        "certificate" in dt["error"].lower(),
        "self-signed cert -> certificate error, not 'not reached'",
        dt["error"],
    )
    ok(
        "verify ssl" in dt["hint"].lower(),
        "self-signed cert -> hint names the Verify SSL toggle",
        dt["hint"],
    )
    ok(
        "certificate_verify_failed" in dt["provider_error"].lower(),
        "self-signed cert -> raw cause kept for support",
        dt["provider_error"],
    )

    dto = llm.describe_transport_error(httpx.ReadTimeout("Read timed out."))
    ok("answer in time" in dto["error"], "timeout -> distinct message", dto["error"])
    ok(
        "timeout" in dto["hint"].lower(),
        "timeout -> hint names the timeout setting",
        dto["hint"],
    )

    ddns = llm.describe_transport_error(
        httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    )
    ok("host name" in ddns["error"], "DNS failure -> host name message", ddns["error"])

    dproto = llm.describe_transport_error(
        httpx.RemoteProtocolError("Server disconnected without sending a response.")
    )
    ok(
        "without a valid reply" in dproto["error"],
        "dropped socket -> protocol message",
        dproto["error"],
    )
    ok(
        "/v1" in dproto["hint"],
        "dropped socket -> hint names the API prefix",
        dproto["hint"],
    )

    dref = llm.describe_transport_error(httpx.ConnectError("Connection refused"))
    ok(
        "refused" in dref["error"],
        "connection refused -> distinct message",
        dref["error"],
    )

    dgen = llm.describe_transport_error(httpx.ConnectError("something unexpected"))
    ok(
        "could not be reached" in dgen["error"],
        "unknown transport failure -> generic fallback",
        dgen["error"],
    )

    print("[8] Copy-paste artefacts in URL and model id are removed")
    ok(
        llm.normalize_base_url("https://nova.deloitte.com.cn/v1`")
        == "https://nova.deloitte.com.cn/v1",
        "trailing back-tick stripped from the base URL",
        llm.normalize_base_url("https://nova.deloitte.com.cn/v1`"),
    )
    ok(
        llm.normalize_base_url("  `https://host/v1/`  ") == "https://host/v1",
        "paired back-ticks and trailing slash stripped",
    )
    ok(
        llm.normalize_model_id(' "DeepSeek-V4-Flash" ') == "DeepSeek-V4-Flash",
        "quotes stripped from the model id, case preserved",
    )
    ok(
        llm.normalize_model_id("`DeepSeek-V4-Flash`") == "DeepSeek-V4-Flash",
        "back-ticks stripped from the model id",
    )
    with get_conn() as conn:
        pid = llm.create_config(
            conn,
            "verify_model_key",
            name=PREFIX + "-paste",
            base_url="https://nova.deloitte.com.cn/v1`",
            api_key=KEY,
            model='"DeepSeek-V4-Flash"',
        )
        CREATED.append(pid)
        prow = conn.execute(
            "SELECT base_url, model FROM model_configs WHERE id=?", (pid,)
        ).fetchone()
        ok(
            prow["base_url"] == "https://nova.deloitte.com.cn/v1",
            "create stored the back-tick-free base URL",
            prow["base_url"],
        )
        ok(
            prow["model"] == "DeepSeek-V4-Flash",
            "create stored the quote-free model id",
            prow["model"],
        )

    print("[9] .env bootstrap migration stores a clean key")
    _check_env_migration()

    # ------------------------------------------------ cleanup
    with get_conn() as conn:
        for cid in CREATED:
            conn.execute("DELETE FROM model_configs WHERE id=?", (cid,))
        left = conn.execute(
            "SELECT COUNT(*) n FROM model_configs WHERE name LIKE ?", (PREFIX + "%",)
        ).fetchone()["n"]
    ok(left == 0, "test configs cleaned up", left)

    print(f"\n  {PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
