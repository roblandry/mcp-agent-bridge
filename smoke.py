# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp>=2.10"]
# ///
"""End-to-end smoke test. Spawns its own server on a temp port + data dir,
exercises every MCP tool + every admin API endpoint, verifies file contents
land on disk (never in DB), peer-token auth, admin-token auth, peer CRUD,
and SOPS-style seed bootstrap.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

HERE = Path(__file__).parent
SERVER = HERE / "server.py"
PORT = 18999
URL = f"http://127.0.0.1:{PORT}/mcp"
HTTP_BASE = f"http://127.0.0.1:{PORT}"
ADMIN_TOKEN = "admin-tok-deadbeefcafef00d12345678abcdef00"
SEED_PEERS = {"claude-laptop": "tok-claude-deadbeef-1234567890abcdef", "openclaw": "tok-clawd-cafef00d-1234567890abcdef"}


def show(label, result):
    if hasattr(result, "data"):
        result = result.data
    print(f"--- {label} ---")
    print(json.dumps(result, indent=2, default=str))
    print()


def http_get(path, admin=True):
    req = urllib.request.Request(HTTP_BASE + path)
    if admin:
        req.add_header("X-Admin-Token", ADMIN_TOKEN)
    return urllib.request.urlopen(req, timeout=3)


def http_json(method, path, body=None, admin=True, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(HTTP_BASE + path, data=data, method=method)
    if admin:
        req.add_header("X-Admin-Token", token or ADMIN_TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            j = json.loads(body)
        except Exception:
            j = {"raw": body.decode("utf-8", "replace")}
        return e.code, j


def wait_ready(timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{HTTP_BASE}/api/status", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception as e:
            last_err = e
        time.sleep(0.3)
    raise RuntimeError(f"server didn't come up: {last_err}")


def mcp_client(peer: str, token: str | None = None) -> Client:
    headers = {"X-Peer-Id": peer}
    if token:
        headers["X-Peer-Token"] = token
    return Client(StreamableHttpTransport(URL, headers=headers))


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agent-bridge-smoke-"))
    print(f"data dir: {tmp}")

    # SOPS-style seed file: a separate readable JSON, NOT inside the data dir
    seed_dir = Path(tempfile.mkdtemp(prefix="agent-bridge-seed-"))
    seed_file = seed_dir / "peers.json"
    seed_file.write_text(json.dumps(SEED_PEERS))
    seed_file.chmod(0o600)

    env = {
        **os.environ,
        "BRIDGE_DATA_DIR": str(tmp),
        "BRIDGE_PORT": str(PORT),
        "BRIDGE_HOST": "127.0.0.1",
        "BRIDGE_PEERS_SEED": str(seed_file),
        "BRIDGE_ADMIN_TOKEN": ADMIN_TOKEN,
    }
    uv = os.environ.get("UV", "uv")
    proc = subprocess.Popen(
        [uv, "run", str(SERVER)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        wait_ready()
        print("server ready\n")

        # ---------- seed bootstrap: peers.json should exist and match seed ----------
        peers_json = tmp / "peers.json"
        assert peers_json.exists(), "peers.json should have been seeded on startup"
        seeded = json.loads(peers_json.read_text())
        assert seeded == SEED_PEERS, f"seed mismatch: got {seeded}"
        print(f"OK: peers.json seeded from {seed_file} on first start\n")

        # ---------- admin auth: status is public, /api/peers needs token ----------
        with http_get("/api/status", admin=False) as r:
            j = json.loads(r.read())
            assert j["admin_auth_required"] is True
            assert j["peer_count"] == 2
        print("OK: /api/status is public and reports correct state\n")

        code, _ = http_json("GET", "/api/peers", admin=False)
        assert code == 401, f"expected 401 without admin token, got {code}"
        code, _ = http_json("GET", "/api/peers", admin=True, token="wrong")
        assert code == 401, f"expected 401 with bad admin token, got {code}"
        code, body = http_json("GET", "/api/peers", admin=True)
        assert code == 200, f"expected 200 with valid admin, got {code}: {body}"
        print("OK: admin auth gates /api/peers (401 missing/bad, 200 valid)\n")

        # ---------- admin peer CRUD ----------
        code, body = http_json("POST", "/api/admin/generate_token")
        assert code == 200 and len(body["token"]) == 64, body
        new_token = body["token"]
        print(f"OK: generate_token returned 64-char hex\n")

        code, body = http_json("POST", "/api/admin/peers", {"id": "new-peer", "token": new_token})
        assert code == 200, body

        code, peers_list = http_json("GET", "/api/admin/peers")
        assert code == 200
        ids = [p["id"] for p in peers_list]
        assert "new-peer" in ids and "claude-laptop" in ids and "openclaw" in ids, ids
        print("OK: upserted new-peer, list_admin_peers reflects all three\n")

        # validate peers.json on disk reflects mutation
        on_disk = json.loads(peers_json.read_text())
        assert on_disk.get("new-peer") == new_token
        print("OK: peers.json on disk updated atomically\n")

        # validation: short token should fail
        code, body = http_json("POST", "/api/admin/peers", {"id": "short", "token": "abc"})
        assert code == 400, body
        # validation: missing fields
        code, body = http_json("POST", "/api/admin/peers", {"id": "only-id"})
        assert code == 400, body
        print("OK: peer upsert validates token length + required fields\n")

        # delete the new peer
        code, _ = http_json("DELETE", f"/api/admin/peers/new-peer")
        assert code == 200
        on_disk = json.loads(peers_json.read_text())
        assert "new-peer" not in on_disk
        print("OK: peer deleted, peers.json reflects removal\n")

        # MCP call with the deleted peer's token should now fail
        deleted_client = mcp_client("new-peer", new_token)
        async with deleted_client:
            try:
                await deleted_client.call_tool("list_peers", {})
                print("FAIL: deleted peer's token should be rejected"); return 1
            except Exception:
                print("OK: deleted peer's MCP token immediately rejected\n")

        # ---------- MCP tools (using seeded peers) ----------
        claude = mcp_client("claude-laptop", SEED_PEERS["claude-laptop"])
        clawd = mcp_client("openclaw", SEED_PEERS["openclaw"])

        async with claude, clawd:
            sent = await claude.call_tool(
                "send_message",
                {"to": "openclaw", "content": "hello from claude", "topic": "smoke"},
            )
            show("send_message", sent)
            inbox = await clawd.call_tool("read_inbox", {})
            assert inbox.data and inbox.data[0]["content"] == "hello from claude"
            print("OK: send/read messaging\n")

            # propose_edit + content on disk
            big_blob = "line " + "x" * 1024 + "\n"
            prop = await claude.call_tool(
                "propose_edit",
                {"file_path": "/tmp/x.txt", "summary": "add a line", "content": big_blob, "target_peer": "openclaw"},
            )
            proposal_id = prop.data["id"]
            with sqlite3.connect(tmp / "bridge.db") as conn:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(proposals)").fetchall()}
                assert "content" not in cols
                fr_cols = {r[1] for r in conn.execute("PRAGMA table_info(file_requests)").fetchall()}
                assert "content" not in fr_cols
            payload = tmp / "payloads" / f"proposal-{proposal_id}"
            assert payload.exists() and payload.read_text() == big_blob
            print("OK: proposal content lives on disk, not in DB\n")

            # only target can apply
            try:
                await claude.call_tool("resolve_proposal", {"proposal_id": proposal_id, "status": "applied"})
                print("FAIL: claude shouldn't apply own proposal"); return 1
            except Exception:
                print("OK: claude blocked from applying own proposal\n")
            await clawd.call_tool("resolve_proposal", {"proposal_id": proposal_id, "status": "applied"})

            # request_file + fulfill
            req = await claude.call_tool(
                "request_file",
                {"file_path": "/etc/hostname", "target_peer": "openclaw", "reason": "want hostname"},
            )
            req_id = req.data["id"]
            try:
                await claude.call_tool("resolve_file_request", {"request_id": req_id, "status": "fulfilled", "content": "spoof"})
                print("FAIL: claude shouldn't fulfill own request"); return 1
            except Exception:
                print("OK: claude blocked from fulfilling own request\n")
            await clawd.call_tool(
                "resolve_file_request",
                {"request_id": req_id, "status": "fulfilled", "content": "openclaw-pod\n"},
            )
            fr_payload = tmp / "payloads" / f"filereq-{req_id}"
            assert fr_payload.exists() and fr_payload.read_text() == "openclaw-pod\n"
            got = await claude.call_tool("get_file_request", {"request_id": req_id})
            assert got.data["content"] == "openclaw-pod\n"
            print("OK: file_request fulfill returns content via on-disk payload\n")

            # deny + withdraw
            r2 = await claude.call_tool("request_file", {"file_path": "/etc/shadow", "target_peer": "openclaw"})
            await clawd.call_tool("resolve_file_request", {"request_id": r2.data["id"], "status": "denied", "note": "no"})
            assert not (tmp / "payloads" / f"filereq-{r2.data['id']}").exists()
            r3 = await claude.call_tool("request_file", {"file_path": "/tmp/whatever", "target_peer": "openclaw"})
            await claude.call_tool("resolve_file_request", {"request_id": r3.data["id"], "status": "withdrawn"})

            # list endpoints don't leak content
            plist = await claude.call_tool("list_proposals", {})
            for p in plist.data:
                assert "content" not in p
            frlist = await claude.call_tool("list_file_requests", {})
            for f in frlist.data:
                assert "content" not in f
            print("OK: list_* endpoints metadata-only\n")

            # size cap
            try:
                await claude.call_tool("send_message", {"to": "openclaw", "content": "x" * (5 * 1024 * 1024 + 1)})
                print("FAIL: oversize accepted"); return 1
            except Exception:
                print("OK: size cap enforced\n")

        # ---------- MCP peer-auth negative cases ----------
        for label, peer, tok in [
            ("bad token", "claude-laptop", "wrong-token"),
            ("missing token", "claude-laptop", None),
            ("unknown peer", "evil-peer", SEED_PEERS["claude-laptop"]),
        ]:
            c = mcp_client(peer, tok) if tok else Client(StreamableHttpTransport(URL, headers={"X-Peer-Id": peer}))
            async with c:
                try:
                    await c.call_tool("list_peers", {})
                    print(f"FAIL: {label} accepted"); return 1
                except Exception:
                    print(f"OK: {label} rejected")
        print()

        # ---------- restart should NOT re-seed if peers.json already exists ----------
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        # mutate peers.json before restart
        current = json.loads(peers_json.read_text())
        current["test-persisted"] = "tok-persisted-1234567890abcdef-EXTRA"
        peers_json.write_text(json.dumps(current))

        # change the seed file to something different — should be ignored
        seed_file.write_text(json.dumps({"should-not-appear": "tok-not-1234567890abcdef-EXTRA"}))

        proc = subprocess.Popen(
            [uv, "run", str(SERVER)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        wait_ready()
        on_disk = json.loads(peers_json.read_text())
        assert "test-persisted" in on_disk, "runtime-added peer was lost on restart"
        assert "should-not-appear" not in on_disk, "seed re-applied even though peers.json existed"
        print("OK: peers.json on PVC survives restart; seed only fires when missing\n")

        print("ALL SMOKE CHECKS PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
