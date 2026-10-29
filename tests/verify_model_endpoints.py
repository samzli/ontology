"""Model configuration endpoints — connection-test contract and full CRUD.

Regression cover for the reported failure: pressing **Test connection** on a
newly added model returned HTTP 400 and the message "The provider could not be
reached", even though the real cause was a self-signed certificate (the
**Verify SSL** toggle) or a rejected key.

What this proves, end to end against a running server:

  1. ``POST /api/admin/models/test`` and ``POST /api/my/models/test`` answer
     **HTTP 200** for every completed test — the verdict lives in ``ok`` plus
     ``error`` / ``hint`` / ``provider_error``. Only genuinely malformed
     requests answer 4xx (missing fields -> 422, unknown id -> 404).
  2. The failure message names the actual cause: a self-signed gateway is
     reported as a certificate problem with the **Verify SSL** hint, not as a
     network problem; unchecking the toggle removes that error.
  3. Admin CRUD: create, duplicate rejection, edit, enable/disable, set
     default, delete, delete-again -> 404. Every step leaves the database and
     the API in agreement.
  4. User CRUD in the Model centre: create, test a saved entry, edit while
     leaving the key blank (it must be kept), delete.
  5. Audit rows are written for model create/update/delete.

Requirements: the server must be running on port 8010 with
``data/onti.db`` seeded (``python main.py``). The test creates its own
models and user, and removes them again.

    python tests/verify_model_endpoints.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import auth  # noqa: E402
from modules.db import get_conn, now  # noqa: E402

BASE = "http://127.0.0.1:8010"
ADMIN_USER = auth.DEFAULT_ADMIN_USERNAME
ADMIN_PW = auth.DEFAULT_ADMIN_PASSWORD

# Two endpoints that fail in *deterministic* ways without needing a real
# provider key: a self-signed gateway, and a port nothing listens on.
NOVA_URL = "https://nova.deloitte.com.cn/v1"
DEAD_URL = "http://127.0.0.1:9/v1"

STAMP = now().replace(":", "").replace("-", "").replace(" ", "")[:14]
PREFIX = "endpointtest" + STAMP
TEMP_USER = "epuser_" + STAMP[-6:].lower()
TEMP_USER_B = TEMP_USER + "b"
TEMP_PW = "Temp@2026x"

PASS = 0
FAIL = 0
RECORDS: list = []


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK  " + label)
    else:
        FAIL += 1
        print("  FAIL " + label + (("  :: " + str(detail)[:400]) if detail else ""))
    RECORDS.append((bool(cond), label))


def cleanup_temp_user() -> None:
    with get_conn() as conn:
        for name in (TEMP_USER, TEMP_USER_B):
            row = conn.execute(
                "SELECT id FROM users WHERE username=?", (name,)
            ).fetchone()
            if row:
                conn.execute(
                    "DELETE FROM user_model_configs WHERE user_id=?", (row["id"],)
                )
                conn.execute("DELETE FROM users WHERE id=?", (row["id"],))


def main() -> int:
    admin = httpx.Client(base_url=BASE, timeout=60)
    user = httpx.Client(base_url=BASE, timeout=60)
    created: list = []
    original_default = None

    try:
        print("[1] Administrator sign-in")
        r = admin.post(
            "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PW}
        )
        ok(r.status_code == 200, "admin login -> 200", r.text[:200])
        if r.status_code != 200:
            print("\nCannot continue without an admin session.")
            return 1

        print("[2] Model list is provider-neutral and complete")
        r = admin.get("/api/admin/models")
        ok(r.status_code == 200, "GET /api/admin/models -> 200", r.status_code)
        models = r.json().get("models", [])
        ok(isinstance(models, list), "model list is a list")
        if models:
            m0 = models[0]
            ok(
                {"id", "name", "base_url", "model", "verify_ssl", "enabled"} <= set(m0),
                "each model exposes id/name/base_url/model/verify_ssl/enabled",
                sorted(m0),
            )
            ok("api_key_enc" not in m0, "raw encrypted key is never serialised")
            original_default = next(
                (m["id"] for m in models if m.get("is_default")), None
            )

        print("[3] A working endpoint answers 200 with ok:true")
        if original_default is not None:
            # The default model is a live gateway: it occasionally queues
            # past the 30 s probe ceiling. A retryable verdict is inconclusive,
            # so retry before drawing a conclusion (same policy the admin
            # console's health sweep uses).
            for attempt in range(1, 4):
                r = admin.post(
                    "/api/admin/models/test", json={"config_id": original_default}
                )
                body = r.json()
                if body.get("ok") or not body.get("retryable"):
                    break
                print(
                    f"        retryable {body.get('category')} — retrying ({attempt}/3)"
                )
                time.sleep(3)
            ok(r.status_code == 200, "stored default model test -> 200", r.status_code)
            ok(body.get("ok") is True, "stored default model test -> ok:true", body)
            ok("latency_ms" in body, "successful test reports latency", body)
            ok(
                body.get("category") is None or isinstance(body.get("category"), str),
                "verdict carries a category",
                body.get("category"),
            )

        print("[4] A failed test is still HTTP 200, with a diagnosis")
        r = admin.post(
            "/api/admin/models/test",
            json={
                "base_url": DEAD_URL,
                "model": "any",
                "api_key": "sk-test",
                "name": "dead",
                "verify_ssl": True,
            },
        )
        body = r.json()
        ok(
            r.status_code == 200,
            "unreachable endpoint test -> 200 (not 400)",
            r.status_code,
        )
        ok(body.get("ok") is False, "unreachable endpoint -> ok:false", body)
        ok(bool(body.get("error")), "unreachable endpoint -> error message", body)
        ok(bool(body.get("hint")), "unreachable endpoint -> actionable hint", body)
        ok(
            bool(body.get("provider_error")),
            "unreachable endpoint -> raw cause kept",
            body,
        )

        print("[5] A self-signed gateway is reported as a certificate problem")
        r = admin.post(
            "/api/admin/models/test",
            json={
                "base_url": NOVA_URL,
                "model": "DeepSeek-V4-Flash",
                "api_key": "ak-test-not-real",
                "name": "nova",
                "verify_ssl": True,
            },
        )
        body = r.json()
        ok(r.status_code == 200, "self-signed gateway test -> 200", r.status_code)
        ok(body.get("ok") is False, "self-signed gateway -> ok:false", body)
        ok(
            "certificate" in (body.get("error") or "").lower(),
            "self-signed gateway -> certificate error, not 'not reached'",
            body.get("error"),
        )
        ok(
            "verify ssl" in (body.get("hint") or "").lower(),
            "self-signed gateway -> hint names the Verify SSL toggle",
            body.get("hint"),
        )
        r = admin.post(
            "/api/admin/models/test",
            json={
                "base_url": NOVA_URL,
                "model": "DeepSeek-V4-Flash",
                "api_key": "ak-test-not-real",
                "name": "nova",
                "verify_ssl": False,
            },
        )
        body = r.json()
        ok(r.status_code == 200, "verify_ssl off -> 200", r.status_code)
        ok(
            "certificate" not in (body.get("error") or "").lower(),
            "verify_ssl off -> the certificate error is gone (key is judged instead)",
            body.get("error"),
        )

        print("[6] Genuinely invalid requests still answer 4xx")
        r = admin.post("/api/admin/models/test", json={"name": "no-endpoint"})
        ok(r.status_code == 422, "missing base_url/model -> 422", r.status_code)
        r = admin.post("/api/admin/models/test", json={"config_id": 99999999})
        ok(r.status_code == 404, "unknown config id -> 404", r.status_code)

        print("[7] Admin CRUD: create")
        payload = {
            "name": PREFIX + "-model",
            "base_url": DEAD_URL,
            "api_key": '  "sk-test-key"  ',
            "model": "test-model-v1  ",
            "temperature": 0.2,
            "max_tokens": 256,
            "timeout": 30,
            "verify_ssl": True,
            "enabled": True,
        }
        r = admin.post("/api/admin/models", json=payload)
        ok(r.status_code == 200, "create model -> 200", r.text[:200])
        mid = r.json().get("id")
        created.append(mid)
        ok(isinstance(mid, int), "create returns the new id", mid)

        r = admin.get("/api/admin/models")
        row = next((m for m in r.json()["models"] if m["id"] == mid), None)
        ok(row is not None, "created model appears in the list")
        ok(
            row and row["model"] == "test-model-v1",
            "model id stored without trailing whitespace",
            row and row["model"],
        )
        ok(
            row and row["base_url"] == DEAD_URL,
            "base URL stored normalised",
            row and row["base_url"],
        )
        ok(row and row["has_api_key"], "key stored (quotes stripped)")
        ok(
            row and row["api_key_masked"].startswith("sk-"),
            "only a masked key is exposed",
            row and row["api_key_masked"],
        )

        r = admin.post("/api/admin/models", json=payload)
        ok(r.status_code == 409, "duplicate name -> 409", r.status_code)

        print("[8] Admin CRUD: edit / enable / default")
        r = admin.put(
            "/api/admin/models/" + str(mid),
            json={
                **payload,
                "name": PREFIX + "-renamed",
                "model": "test-model-v2",
                "verify_ssl": False,
                "enabled": False,
                "api_key": "__unchanged__",
            },
        )
        ok(r.status_code == 200, "edit model -> 200", r.text[:200])
        row = next(
            (
                m
                for m in admin.get("/api/admin/models").json()["models"]
                if m["id"] == mid
            ),
            None,
        )
        ok(row and row["name"] == PREFIX + "-renamed", "edit applied the new name")
        ok(row and row["model"] == "test-model-v2", "edit applied the new model id")
        ok(row and row["verify_ssl"] is False, "edit applied verify_ssl=false")
        ok(row and row["enabled"] is False, "edit applied enabled=false")
        ok(row and row["has_api_key"], "blank/__unchanged__ key is preserved")

        r = admin.post("/api/admin/models/test", json={"config_id": mid})
        ok(
            r.status_code == 200 and r.json().get("ok") is False,
            "disabled model can still be tested (200, ok:false)",
            r.status_code,
        )
        ok("api_key" not in r.json(), "failed test never echoes the key back")

        r = admin.post("/api/admin/models/" + str(mid) + "/default")
        ok(r.status_code == 200, "set default -> 200", r.status_code)
        row = next(
            (
                m
                for m in admin.get("/api/admin/models").json()["models"]
                if m["id"] == mid
            ),
            None,
        )
        ok(row and row["is_default"], "model is now the default")
        if original_default is not None:
            r = admin.post("/api/admin/models/" + str(original_default) + "/default")
            ok(r.status_code == 200, "restore previous default -> 200", r.status_code)
            restored = next(
                (
                    m
                    for m in admin.get("/api/admin/models").json()["models"]
                    if m["id"] == original_default
                ),
                None,
            )
            ok(restored and restored["is_default"], "previous default restored")

        print("[9] Admin CRUD: delete")
        r = admin.delete("/api/admin/models/" + str(mid))
        ok(r.status_code == 200, "delete model -> 200", r.status_code)
        ids = [m["id"] for m in admin.get("/api/admin/models").json()["models"]]
        ok(mid not in ids, "deleted model is gone from the list")
        r = admin.delete("/api/admin/models/" + str(mid))
        ok(r.status_code == 404, "delete again -> 404", r.status_code)
        r = admin.post("/api/admin/models/test", json={"config_id": mid})
        ok(r.status_code == 404, "test a deleted model -> 404", r.status_code)
        created.remove(mid)

        print("[10] User Model centre: create / test / edit / delete")
        cleanup_temp_user()
        with get_conn() as conn:
            auth.create_user(conn, TEMP_USER, TEMP_PW, "Endpoint Test User", "user")
        r = user.post(
            "/api/auth/login", json={"username": TEMP_USER, "password": TEMP_PW}
        )
        ok(r.status_code == 200, "temp user login -> 200", r.status_code)

        r = user.get("/api/my/models")
        ok(
            r.status_code == 200 and r.json()["models"] == [],
            "new user starts with no personal models",
            r.text[:200],
        )

        r = user.post(
            "/api/my/models",
            json={
                "name": PREFIX + "-usermodel",
                "base_url": DEAD_URL,
                "api_key": "sk-user-test",
                "model": "user-model-v1",
                "verify_ssl": True,
                "write_env": False,
            },
        )
        ok(r.status_code == 200, "user create model -> 200", r.text[:200])
        uid = r.json().get("id")
        ok(isinstance(uid, int), "user create returns the id", uid)

        r = user.post("/api/my/models/test", json={"config_id": "my:" + str(uid)})
        body = r.json()
        ok(
            r.status_code == 200,
            "user test saved model -> 200 (not 400)",
            r.status_code,
        )
        ok(body.get("ok") is False, "user test reports ok:false", body)
        ok(bool(body.get("hint")), "user test carries a hint", body)

        r = user.put(
            "/api/my/models/" + str(uid),
            json={
                "name": PREFIX + "-usermodel",
                "base_url": DEAD_URL,
                "api_key": "",
                "model": "user-model-v2",
                "verify_ssl": True,
                "write_env": False,
            },
        )
        ok(r.status_code == 200, "user edit with a blank key -> 200", r.text[:200])
        row = next(
            (m for m in user.get("/api/my/models").json()["models"] if m["id"] == uid),
            None,
        )
        ok(
            row and row["model"] == "user-model-v2",
            "user edit applied the new model id",
        )
        ok(row and row["has_api_key"], "blank key on edit keeps the stored key")

        # Ownership boundary: a personal model belongs to its owner only.
        with get_conn() as conn:
            auth.create_user(conn, TEMP_USER_B, TEMP_PW, "Other User", "user")
        other = httpx.Client(base_url=BASE, timeout=60)
        try:
            r = other.post(
                "/api/auth/login",
                json={"username": TEMP_USER_B, "password": TEMP_PW},
            )
            ok(r.status_code == 200, "second user login -> 200", r.status_code)
            ok(
                other.get("/api/my/models").json()["models"] == [],
                "second user does not see the first user's model",
            )
            r = other.put(
                "/api/my/models/" + str(uid),
                json={
                    "name": PREFIX + "-usermodel",
                    "base_url": DEAD_URL,
                    "api_key": "",
                    "model": "hijack",
                    "verify_ssl": True,
                    "write_env": False,
                },
            )
            ok(
                r.status_code == 404,
                "other user editing that model -> 404",
                r.status_code,
            )
            r = other.delete("/api/my/models/" + str(uid))
            ok(
                r.status_code == 404,
                "other user deleting that model -> 404",
                r.status_code,
            )
        finally:
            other.close()

        r = user.delete("/api/my/models/" + str(uid))
        ok(r.status_code == 200, "user delete model -> 200", r.status_code)
        ok(
            user.get("/api/my/models").json()["models"] == [],
            "user model list is empty again",
        )

        print("[11] Audit trail")
        r = admin.get("/api/admin/audit?limit=60")
        kinds = " ".join(str(e.get("action", "")) for e in r.json().get("events", []))
        ok("model.create" in kinds, "audit records model.create", kinds[:160])
        ok("model.update" in kinds, "audit records model.update", kinds[:160])
        ok("model.delete" in kinds, "audit records model.delete", kinds[:160])

    finally:
        for mid in created:
            try:
                admin.delete("/api/admin/models/" + str(mid))
            except Exception:  # noqa: BLE001
                pass
        cleanup_temp_user()
        admin.close()
        user.close()

    print("\n" + "-" * 64)
    print(f"passed {PASS} / {PASS + FAIL} checks")
    if FAIL:
        print("FAILURES:")
        for good, label in RECORDS:
            if not good:
                print("  - " + label)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
