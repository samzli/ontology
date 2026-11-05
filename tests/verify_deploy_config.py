"""Deployment configuration regression — vercel.json and config switching.

Guards the two failure modes that broke production before:

* a catch-all ``rewrites`` entry in ``vercel.json`` (Vercel already routes
  every path to the detected FastAPI app, so the rewrite replaced the
  request path and every route answered ``{"detail":"Not Found"}``);
* configuration that cannot be switched per environment.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ FAIL {label}  :: {detail}")


def main() -> int:
    print("=== Deployment configuration regression ===")

    # [1] vercel.json is valid, minimal and free of deprecated keys
    raw = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    ok(isinstance(raw, dict), "[1] vercel.json parses as an object")
    ok("builds" not in raw, "[1] no deprecated builds section")
    ok("routes" not in raw, "[1] no deprecated routes section")
    ok("rewrites" not in raw, "[1] no catch-all rewrites (breaks FastAPI routing)")
    ok(
        raw.get("functions", {}).get("main.py", {}).get("maxDuration") == 60,
        "[1] functions.main.py.maxDuration is 60",
        raw.get("functions"),
    )
    cron_paths = [c.get("path") for c in raw.get("crons", [])]
    ok(
        "/api/internal/snapshot-sync" in cron_paths,
        "[1] daily snapshot cron registered",
        cron_paths,
    )

    # [2] snapshot settings live in the config file; secrets stay in env
    import modules.config as cfg

    snap = cfg.CONFIG.get("snapshot", {})
    required = {
        "blob_api_base",
        "blob_api_version",
        "prefix",
        "timeout_s",
        "min_interval_s",
        "blob_token_env",
        "http_url_env",
        "http_token_env",
    }
    ok(required <= set(snap), "[2] snapshot config keys present", sorted(snap))
    ok(
        not any("TOKEN" in str(v) and len(str(v)) > 40 for v in snap.values()),
        "[2] no secret values inline (only env var names)",
    )

    # [3] ONTI_CONFIG points the loader at another file
    tmpdir = Path(tempfile.mkdtemp())
    alt = tmpdir / "config.test.json"
    alt.write_text(
        json.dumps({"system_name": "AltEnv", "server": {"port": 9999}}),
        encoding="utf-8",
    )
    saved = os.environ.get("ONTI_CONFIG")
    os.environ["ONTI_CONFIG"] = str(alt)
    try:
        importlib.reload(cfg)
        ok(cfg.SYSTEM_NAME == "AltEnv", "[3] ONTI_CONFIG switches the file")
        ok(cfg.PORT == 9999, "[3] values come from the alternate file", cfg.PORT)
    finally:
        if saved is None:
            os.environ.pop("ONTI_CONFIG", None)
        else:
            os.environ["ONTI_CONFIG"] = saved
        importlib.reload(cfg)

    # [4] environment overlays deep-merge instead of replacing
    merged = cfg._deep_merge(
        {"server": {"port": 8010, "host": "0.0.0.0"}, "system_name": "Onti"},
        {"server": {"port": 8020}},
    )
    ok(
        merged["server"] == {"port": 8020, "host": "0.0.0.0"}
        and merged["system_name"] == "Onti",
        "[4] overlay keeps untouched keys",
        merged,
    )

    # [5] the snapshot backend is opt-in through the env vars named in config
    from modules import snapshot

    for var in (
        snap.get("blob_token_env"),
        snap.get("http_url_env"),
        snap.get("http_token_env"),
    ):
        os.environ.pop(var, None)
    ok(snapshot.backend() == "", "[5] disabled without env")
    os.environ[snap["http_url_env"]] = "http://127.0.0.1:9/snapshot"
    try:
        ok(snapshot.backend() == "http", "[5] http backend from config env name")
    finally:
        os.environ.pop(snap["http_url_env"], None)

    print()
    print(f"Deploy config: {PASS} ok / {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
