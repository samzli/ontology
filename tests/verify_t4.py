"""T4 verification: generic, admin-configurable OpenAI-compatible model center."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.db import get_conn
from modules import llm_client as ml

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  BAD {name} :: {detail}")


with get_conn() as conn:
    print("[TR-4.1] configuration center CRUD + validation")
    configs = ml.list_configs(conn)
    check("at least one configured model", len(configs) >= 1, str(len(configs)))
    seed_cfg = next(c for c in configs if c["is_default"])
    check("seed config is default", seed_cfg["is_default"])
    check(
        "key is masked in public view",
        seed_cfg["api_key_masked"].startswith("sk-")
        and "*" in seed_cfg["api_key_masked"],
        seed_cfg["api_key_masked"],
    )

    raw_row = conn.execute(
        "SELECT api_key_enc FROM model_configs WHERE id=?", (seed_cfg["id"],)
    ).fetchone()["api_key_enc"]
    from modules.db import decrypt_secret

    plain_key = decrypt_secret(raw_row)
    check(
        "plaintext key never stored in DB",
        plain_key and plain_key != raw_row and plain_key not in raw_row,
        raw_row[:30],
    )

    # Create second (generic, non-SF) model — platform is provider agnostic.
    cid2 = ml.create_config(
        conn,
        "rootadmin",
        name="Local vLLM",
        base_url="http://127.0.0.1:8000/v1/",
        api_key="not-a-secret",
        model="meta-llama/Llama-3.1-8B-Instruct",
        temperature=0.1,
        max_tokens=512,
        timeout=20,
        enabled=False,
    )
    check(
        "create config + trailing slash stripped",
        conn.execute(
            "SELECT base_url FROM model_configs WHERE id=?", (cid2,)
        ).fetchone()["base_url"]
        == "http://127.0.0.1:8000/v1",
    )

    try:
        ml.create_config(
            conn,
            "rootadmin",
            name="Local vLLM",
            base_url="http://x/v1",
            api_key="k",
            model="m",
        )
        check("duplicate name -> 409", False)
    except ml.ModelConfigError as e:
        check("duplicate name -> 409", e.status_code == 409, str(e.status_code))

    for bad in (
        ("  ", "http://x/v1", "m"),
        ("n", "ftp://x", "m"),
        ("n", "http://x/v1", ""),
    ):
        try:
            ml.create_config(
                conn,
                "rootadmin",
                name=bad[0],
                base_url=bad[1],
                api_key="k",
                model=bad[2],
            )
            check("invalid payload rejected " + str(bad), False)
        except ml.ModelConfigError as e:
            check(
                "invalid payload rejected (" + bad[0] or "?" + ")", e.status_code == 422
            )

    ml.update_config(conn, "rootadmin", cid2, {"model": "qwen2.5:7b", "enabled": True})
    got = conn.execute(
        "SELECT model, enabled FROM model_configs WHERE id=?", (cid2,)
    ).fetchone()
    check("update fields", got["model"] == "qwen2.5:7b" and got["enabled"] == 1)
    ml.update_config(conn, "rootadmin", cid2, {"api_key": "rotated-key-999"})
    rotated = next(c for c in ml.list_configs(conn) if c["id"] == cid2)
    check(
        "key rotation decrypts",
        rotated["has_api_key"]
        and conn.execute(
            "SELECT 1 FROM model_configs WHERE id=? AND api_key_enc NOT LIKE '%rotated-key%'",
            (cid2,),
        ).fetchone()
        is not None,
    )

    ml.set_default(conn, "rootadmin", cid2)
    check("default switched", ml.list_configs(conn)[0]["id"] == cid2)

    print("[TR-4.2] runtime resolution")
    cfg = ml.get_default_client_config(conn)
    check(
        "resolves new default with decrypted key",
        cfg and cfg["id"] == cid2 and cfg["api_key"] == "rotated-key-999",
    )
    ml.set_default(conn, "rootadmin", seed_cfg["id"])
    ml.update_config(conn, "rootadmin", cid2, {"enabled": False})
    cfg = ml.get_default_client_config(conn)
    check(
        "default resolves, disabled model skipped", cfg and cfg["id"] == seed_cfg["id"]
    )

    ml.log_call(
        conn,
        user_id=None,
        config_id=seed_cfg["id"],
        model=seed_cfg["model"],
        success=True,
        latency_ms=12.5,
        tokens=3,
    )
    check(
        "llm_calls audit row",
        conn.execute("SELECT COUNT(*) n FROM llm_calls WHERE success=1").fetchone()["n"]
        >= 1,
    )

    print("[TR-4.3] connection test never crashes; structured failure for bad endpoint")
    bad_client = ml.ConfiguredLLM(
        {
            "name": "broken",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "k",
            "model": "m",
            "timeout": 3,
        }
    )
    result = asyncio.run(bad_client.test_connection())
    check(
        "bad endpoint -> structured error",
        result["status"] == "error"
        and isinstance(result["error"], str)
        and "latency_ms" in result,
        str(result)[:120],
    )

    print("[TR-4.4] sync answer to bad endpoint returns error dict (no exception)")
    r = bad_client.answer("sys", "hi")
    check("sync answer error envelope", r["status"] == "error" and bool(r["error"]))

    ml.delete_config(conn, "rootadmin", cid2)
    check(
        "delete works",
        conn.execute("SELECT 1 FROM model_configs WHERE id=?", (cid2,)).fetchone()
        is None,
    )
    remaining = ml.list_configs(conn)
    defaults = [c for c in remaining if c["is_default"]]
    check(
        "exactly one default model survives the delete",
        len(defaults) == 1 and defaults[0]["id"] == seed_cfg["id"],
        str([c["id"] for c in defaults]),
    )

    audits = conn.execute(
        "SELECT COUNT(*) n FROM audit_log WHERE action LIKE 'model.%'"
    ).fetchone()["n"]
    check("model admin actions audited", audits >= 5, str(audits))

print("[TR-4.5] no hardcoded provider/model outside seed migration")
import pathlib

root = pathlib.Path(__file__).resolve().parent.parent
offenders = []
for p in (root / "modules").glob("*.py"):
    if p.name == "seed.py":
        continue
    txt = p.read_text(encoding="utf-8")
    for token in ("deepseek-ai/DeepSeek", "DEEPSEEK_API_KEY"):
        if token in txt:
            offenders.append(f"{p.name}:{token}")
check("no model string/key hardcoded in runtime modules", not offenders, str(offenders))

print()
print(f"T4 verification: {'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
