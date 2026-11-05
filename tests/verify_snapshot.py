"""Snapshot sync regression — offline unit checks plus a local HTTP round-trip.

No external service is required: the round-trip uses a throwaway in-process
HTTP stub standing in for ``ONTI_SNAPSHOT_URL``, so the suite stays hermetic.
"""

from __future__ import annotations

import http.server
import os
import sys
import tempfile
import threading
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


class _Stub(http.server.BaseHTTPRequestHandler):
    """Minimal snapshot endpoint: PUT stores bytes, GET returns them."""

    store = b""

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        _Stub.store = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if not _Stub.store:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(_Stub.store)))
        self.end_headers()
        self.wfile.write(_Stub.store)

    def log_message(self, *args):  # keep the test output clean
        pass


def main() -> int:
    from modules import snapshot

    print("=== Snapshot sync regression ===")

    # [1] disabled by default (no env in the test runner)
    for var in ("BLOB_READ_WRITE_TOKEN", "ONTI_SNAPSHOT_URL", "ONTI_SNAPSHOT_TOKEN"):
        os.environ.pop(var, None)
    ok(snapshot.backend() == "", "[1] no backend without env", snapshot.backend())
    ok(not snapshot.enabled(), "[1] disabled without env")

    # [2] disabled calls are safe no-ops
    ok(snapshot.sync_now() is False, "[2] sync_now no-op when disabled")
    ok(snapshot.request_sync() is False, "[2] request_sync no-op when disabled")
    tmp = Path(tempfile.mkdtemp()) / "onti.db"
    ok(
        snapshot.restore_latest(tmp) is False and not tmp.exists(),
        "[2] restore_latest no-op when disabled",
    )

    # [3] SQLite magic check guards against restoring garbage
    ok(
        snapshot._looks_like_sqlite(b"SQLite format 3\x00rest"),
        "[3] magic accepts SQLite header",
    )
    ok(
        not snapshot._looks_like_sqlite(b"not a database"),
        "[3] magic rejects junk",
    )

    # [4] full round-trip against the local HTTP stub
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/snapshot"
    os.environ["ONTI_SNAPSHOT_URL"] = url
    os.environ["ONTI_SNAPSHOT_TOKEN"] = "test-token"
    try:
        ok(snapshot.backend() == "http", "[4] http backend detected", url)
        ok(snapshot.enabled(), "[4] enabled with ONTI_SNAPSHOT_URL")
        ok(snapshot.sync_now() is True, "[4] sync_now uploads the live db")
        ok(
            _Stub.store[:16] == b"SQLite format 3\x00",
            "[4] uploaded bytes are a SQLite db",
            _Stub.store[:16],
        )
        target = Path(tempfile.mkdtemp()) / "restored.db"
        ok(
            snapshot.restore_latest(target) is True,
            "[4] restore_latest downloads the snapshot",
        )
        ok(
            target.exists() and target.read_bytes() == _Stub.store,
            "[4] restored bytes match the upload",
        )

        # [5] burst collapse: a request_sync right after sync_now is skipped
        ok(
            snapshot.request_sync() is False,
            "[5] request_sync collapses bursts (<5s since last sync)",
        )

        # [6] junk in the store is rejected by the magic check
        _Stub.store = b"<html>not a db</html>"
        bad = Path(tempfile.mkdtemp()) / "bad.db"
        ok(
            snapshot.restore_latest(bad) is False and not bad.exists(),
            "[6] corrupt snapshot is rejected",
        )
    finally:
        server.shutdown()
        for var in ("ONTI_SNAPSHOT_URL", "ONTI_SNAPSHOT_TOKEN"):
            os.environ.pop(var, None)

    print()
    print(f"Snapshot: {PASS} ok / {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
