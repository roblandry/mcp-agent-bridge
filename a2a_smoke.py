# /// script
# requires-python = ">=3.11"
# dependencies = ["starlette>=0.40", "uvicorn>=0.30"]
# ///
"""Smoke test for the A2A relay prototype."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
SERVER = HERE / "a2a_bridge.py"
PORT = 19081
BASE = f"http://127.0.0.1:{PORT}"
ADMIN_TOKEN = "admin-a2a-deadbeefcafef00d1234567890"
PEERS = {
    "claude-pc": "tok-claude-a2a-deadbeefcafef00d",
    "openclaw": "tok-openclaw-a2a-deadbeefcafef00d",
    "hermes": "tok-hermes-a2a-deadbeefcafef00d",
}


def request_json(
    method: str,
    path: str,
    body: Any | None = None,
    *,
    peer: str | None = None,
    admin: bool = False,
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if peer:
        req.add_header("X-Peer-Id", peer)
        req.add_header("X-Peer-Token", PEERS[peer])
    if admin:
        req.add_header("X-Admin-Token", ADMIN_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            raw = r.read()
            return r.status, json.loads(raw or "null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw.decode("utf-8", "replace")}


def wait_ready(timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            code, body = request_json("GET", "/api/status")
            if code == 200 and body.get("ok"):
                return
        except Exception as e:
            last = e
        time.sleep(0.2)
    raise RuntimeError(f"server did not become ready: {last}")


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="a2a-bridge-smoke-"))
    seed = data_dir / "seed-peers.json"
    seed.write_text(json.dumps(PEERS))
    env = {
        **os.environ,
        "BRIDGE_DATA_DIR": str(data_dir),
        "BRIDGE_PEERS_SEED": str(seed),
        "BRIDGE_ADMIN_TOKEN": ADMIN_TOKEN,
        "BRIDGE_HOST": "127.0.0.1",
        "BRIDGE_PORT": str(PORT),
        "A2A_PUBLIC_BASE_URL": BASE,
    }
    proc = subprocess.Popen(
        [os.environ.get("UV", "uv"), "run", str(SERVER)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_ready()
        print("OK: server ready")

        code, card = request_json("GET", "/agents/openclaw/.well-known/agent-card.json")
        assert code == 200, card
        assert card["name"] == "openclaw"
        assert card["supportedInterfaces"][0]["url"] == f"{BASE}/agents/openclaw"
        print("OK: agent card")

        code, fail = request_json(
            "POST",
            "/agents/openclaw/message:send",
            {"message": {"role": "ROLE_USER", "parts": [{"text": "hello from claude"}]}},
        )
        assert code == 401, fail
        print("OK: peer auth required")

        code, sent = request_json(
            "POST",
            "/agents/openclaw/message:send",
            {
                "message": {
                    "role": "ROLE_USER",
                    "messageId": "msg-1",
                    "contextId": "ctx-demo",
                    "metadata": {"topic": "demo"},
                    "parts": [{"text": "OpenClaw, ask Hermes for a quick status."}],
                }
            },
            peer="claude-pc",
        )
        assert code == 200, sent
        task = sent["task"]
        task_id = task["id"]
        assert task["contextId"] == "ctx-demo"
        assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
        print("OK: sender created A2A task")

        code, inbox = request_json("GET", "/api/agent/inbox", peer="openclaw")
        assert code == 200, inbox
        assert inbox["tasks"][0]["id"] == task_id
        print("OK: target sees inbox task")

        code, working = request_json(
            "POST",
            f"/api/agent/tasks/{task_id}/events",
            {"state": "TASK_STATE_WORKING", "text": "I am checking with Hermes now."},
            peer="openclaw",
        )
        assert code == 200, working
        print("OK: target posted progress")

        code, completed = request_json(
            "POST",
            f"/api/agent/tasks/{task_id}/events",
            {"state": "TASK_STATE_COMPLETED", "text": "Hermes reports nominal status."},
            peer="openclaw",
        )
        assert code == 200, completed
        assert completed["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        print("OK: target completed task")

        code, fetched = request_json("GET", f"/agents/openclaw/tasks/{task_id}", peer="claude-pc")
        assert code == 200, fetched
        assert fetched["task"]["artifacts"][0]["parts"][0]["text"] == "Hermes reports nominal status."
        print("OK: sender fetched final artifact")

        code, convs = request_json("GET", "/api/conversations", admin=True)
        assert code == 200, convs
        assert convs["conversations"][0]["contextId"] == "ctx-demo"
        code, detail = request_json("GET", "/api/conversations/ctx-demo", admin=True)
        assert code == 200, detail
        assert [e["actorPeer"] for e in detail["events"]] == ["claude-pc", "openclaw", "openclaw"]
        assert "Hermes reports nominal" in detail["events"][-1]["text"]
        print("OK: admin transcript visible")

        print("\nA2A smoke passed")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.returncode not in {0, -15, None} and proc.stdout:
            print(proc.stdout.read(), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
