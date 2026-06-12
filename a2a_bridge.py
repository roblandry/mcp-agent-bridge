# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "starlette>=0.40",
#   "uvicorn>=0.30",
# ]
# ///
"""
Small A2A-style relay for private agent-to-agent collaboration.

The bridge exposes one A2A-ish endpoint per peer:

  POST /agents/{peer_id}/message:send
  GET  /agents/{peer_id}/tasks/{task_id}
  POST /agents/{peer_id}/tasks/{task_id}:subscribe
  GET  /agents/{peer_id}/.well-known/agent-card.json

Agents authenticate with X-Peer-Id / X-Peer-Token. A sender posts to the
target peer's /message:send URL. The target peer polls /api/agent/inbox and
posts progress/completion events through /api/agent/tasks/{task_id}/events.

This is intentionally a narrow pilot, not a full A2A server implementation.
It keeps the protocol surface close to A2A while adding the missing local
ergonomics Rob wants: a human-visible transcript of agent conversations.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import os
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable, Optional, cast

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

DATA_DIR = Path(os.environ.get("BRIDGE_DATA_DIR", str(Path(__file__).parent)))
DB_PATH = DATA_DIR / "a2a_bridge.db"
PEERS_FILE = DATA_DIR / "peers.json"
HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
PUBLIC_BASE_URL = (os.environ.get("A2A_PUBLIC_BASE_URL") or "").rstrip("/")
BRIDGE_VERSION = (os.environ.get("BRIDGE_VERSION") or "dev").strip()
ADMIN_TOKEN = (os.environ.get("BRIDGE_ADMIN_TOKEN") or "").strip() or None
PEERS_SEED_PATH = os.environ.get("BRIDGE_PEERS_SEED") or os.environ.get("BRIDGE_PEERS_FILE")
MAX_TEXT_BYTES = int(os.environ.get("A2A_BRIDGE_MAX_TEXT_BYTES", str(512 * 1024)))

TERMINAL_STATES: Final[set[str]] = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}
ACTIVE_STATES: Final[set[str]] = {
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
}
VALID_STATES: Final[set[str]] = TERMINAL_STATES | ACTIVE_STATES | {"TASK_STATE_UNSPECIFIED"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        DATA_DIR.chmod(0o700)
    except PermissionError:
        pass


def load_peers_from_disk() -> dict[str, str]:
    if not PEERS_FILE.exists():
        return {}
    try:
        data: Any = json.loads(PEERS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"a2a-bridge: WARN failed to read {PEERS_FILE}: {e}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k).strip(): str(v).strip()
        for k, v in cast("dict[Any, Any]", data).items()
        if str(k).strip() and str(v).strip()
    }


def save_peers_atomic(peers: dict[str, str]) -> None:
    tmp = PEERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(peers, indent=2, sort_keys=True))
    try:
        tmp.chmod(0o600)
    except PermissionError:
        pass
    tmp.replace(PEERS_FILE)


PEERS: Final[dict[str, str]] = {}


def reload_peers() -> None:
    PEERS.clear()
    PEERS.update(load_peers_from_disk())


def bootstrap_peers() -> None:
    if PEERS_FILE.exists() or not PEERS_SEED_PATH:
        return
    seed = Path(PEERS_SEED_PATH)
    if not seed.exists():
        print(f"a2a-bridge: WARN seed path {seed} does not exist", file=sys.stderr)
        return
    try:
        data: Any = json.loads(seed.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"a2a-bridge: WARN failed to read seed {seed}: {e}", file=sys.stderr)
        return
    if not isinstance(data, dict):
        return
    peers = {
        str(k).strip(): str(v).strip()
        for k, v in cast("dict[Any, Any]", data).items()
        if str(k).strip() and str(v).strip()
    }
    if peers:
        save_peers_atomic(peers)
        print(f"a2a-bridge: seeded {len(peers)} peer(s) from {seed}", file=sys.stderr)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS peers (
              id TEXT PRIMARY KEY,
              last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              context_id TEXT NOT NULL,
              from_peer TEXT NOT NULL,
              to_peer TEXT NOT NULL,
              state TEXT NOT NULL,
              input_text TEXT NOT NULL,
              output_text TEXT,
              topic TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              claimed_at TEXT,
              completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id TEXT NOT NULL,
              context_id TEXT NOT NULL,
              actor_peer TEXT NOT NULL,
              role TEXT NOT NULL,
              kind TEXT NOT NULL,
              state TEXT,
              text TEXT,
              artifact_json TEXT,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_to_state ON tasks(to_peer, state, updated_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_context ON tasks(context_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
            """
        )


def touch_peer(peer_id: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO peers(id, last_seen) VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen
            """,
            (peer_id, now_iso()),
        )


def authenticate_peer(request: Request) -> str:
    peer_id = (request.headers.get("X-Peer-Id") or "").strip()
    token = (request.headers.get("X-Peer-Token") or "").strip()
    expected = PEERS.get(peer_id)
    if not peer_id or not token or not expected or not hmac.compare_digest(token, expected):
        raise PermissionError("invalid peer credentials")
    touch_peer(peer_id)
    return peer_id


def require_admin(request: Request) -> None:
    if not ADMIN_TOKEN:
        return
    supplied = (request.headers.get("X-Admin-Token") or request.query_params.get("token") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, ADMIN_TOKEN):
        raise PermissionError("admin token required")


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def task_to_a2a(row: sqlite3.Row, include_history: bool = False) -> dict[str, Any]:
    task = {
        "id": row["id"],
        "contextId": row["context_id"],
        "status": {
            "state": row["state"],
            "timestamp": row["updated_at"],
        },
        "metadata": {
            "fromPeer": row["from_peer"],
            "toPeer": row["to_peer"],
            "topic": row["topic"],
        },
        "artifacts": [],
    }
    if row["output_text"]:
        task["artifacts"].append(
            {
                "artifactId": f"{row['id']}:response",
                "name": "response",
                "parts": [{"text": row["output_text"], "mediaType": "text/plain"}],
            }
        )
    if include_history:
        task["history"] = [
            {
                "role": "ROLE_USER",
                "parts": [{"text": row["input_text"], "mediaType": "text/plain"}],
                "messageId": f"{row['id']}:input",
                "contextId": row["context_id"],
                "taskId": row["id"],
            }
        ]
    return task


def add_event(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    context_id: str,
    actor_peer: str,
    role: str,
    kind: str,
    text: str | None = None,
    state: str | None = None,
    artifact: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events(task_id, context_id, actor_peer, role, kind, state, text, artifact_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            context_id,
            actor_peer,
            role,
            kind,
            state,
            text,
            json.dumps(artifact) if artifact else None,
            now_iso(),
        ),
    )


def extract_text_from_message(message: dict[str, Any]) -> str:
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
        elif isinstance(part, dict) and "data" in part:
            chunks.append(json.dumps(part["data"], ensure_ascii=False))
    text = "\n".join(chunks).strip()
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("message text too large")
    return text


def base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    return str(request.base_url).rstrip("/")


async def status(request: Request) -> Response:
    with db() as conn:
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return JSONResponse(
        {
            "ok": True,
            "mode": "a2a-relay",
            "version": BRIDGE_VERSION,
            "peerCount": len(PEERS),
            "taskCount": task_count,
            "eventCount": event_count,
            "adminAuthRequired": ADMIN_TOKEN is not None,
        }
    )


async def agent_card(request: Request) -> Response:
    peer_id = request.path_params["peer_id"]
    if peer_id not in PEERS:
        return error(404, "unknown peer")
    url = f"{base_url(request)}/agents/{peer_id}"
    return JSONResponse(
        {
            "name": peer_id,
            "description": f"Private relay endpoint for peer {peer_id}.",
            "version": BRIDGE_VERSION,
            "provider": {"organization": "OpenClaw", "url": base_url(request)},
            "capabilities": {"streaming": True, "pushNotifications": False},
            "securitySchemes": {
                "peerToken": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Peer-Token",
                    "description": "Pair with X-Peer-Id for bridge authentication.",
                }
            },
            "securityRequirements": [{"peerToken": []}],
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "application/json"],
            "supportedInterfaces": [
                {"url": url, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}
            ],
            "skills": [
                {
                    "id": "collaborate",
                    "name": "Collaborate with this peer",
                    "description": "Send a task/message to the peer and receive progress or a final artifact.",
                    "tags": ["coordination", "handoff", "a2a-relay"],
                    "examples": ["Please inspect this issue and report back."],
                    "inputModes": ["text/plain", "application/json"],
                    "outputModes": ["text/plain", "application/json"],
                }
            ],
        }
    )


async def catalog_card(request: Request) -> Response:
    url = base_url(request)
    return JSONResponse(
        {
            "name": "OpenClaw A2A Relay",
            "description": "Private A2A relay exposing configured peers as agent endpoints.",
            "version": BRIDGE_VERSION,
            "provider": {"organization": "OpenClaw", "url": url},
            "capabilities": {"streaming": True, "pushNotifications": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "application/json"],
            "supportedInterfaces": [
                {"url": url, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}
            ],
            "skills": [
                {
                    "id": "relay-catalog",
                    "name": "List relay peers",
                    "description": "Discover private peer Agent Card URLs from the relay.",
                    "tags": ["discovery", "relay"],
                    "inputModes": ["application/json"],
                    "outputModes": ["application/json"],
                }
            ],
            "metadata": {
                "peers": [
                    {"id": peer_id, "agentCardUrl": f"{url}/agents/{peer_id}/.well-known/agent-card.json"}
                    for peer_id in sorted(PEERS)
                ]
            },
        }
    )


async def send_message(request: Request) -> Response:
    try:
        from_peer = authenticate_peer(request)
    except PermissionError as e:
        return error(401, str(e))
    to_peer = request.path_params["peer_id"]
    if to_peer not in PEERS:
        return error(404, "unknown target peer")
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("message"):
            message = cast("dict[str, Any]", body["message"])
        elif isinstance(body, dict) and isinstance(body.get("params"), dict):
            message = cast("dict[str, Any]", body["params"].get("message") or {})
        else:
            message = {}
        text = extract_text_from_message(message)
    except ValueError as e:
        return error(413, str(e))
    except Exception:
        return error(400, "invalid A2A message")
    if not text:
        return error(400, "message must include a text or data part")

    task_id = str(uuid.uuid4())
    context_id = str(message.get("contextId") or str(uuid.uuid4()))
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    topic = str(metadata.get("topic") or context_id)
    ts = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO tasks(id, context_id, from_peer, to_peer, state, input_text, topic, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, context_id, from_peer, to_peer, "TASK_STATE_SUBMITTED", text, topic, ts, ts),
        )
        add_event(
            conn,
            task_id=task_id,
            context_id=context_id,
            actor_peer=from_peer,
            role="ROLE_USER",
            kind="message",
            text=text,
            state="TASK_STATE_SUBMITTED",
        )
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return JSONResponse(
        {"task": task_to_a2a(row, include_history=True)},
        media_type="application/a2a+json",
    )


async def get_task(request: Request) -> Response:
    try:
        peer_id = authenticate_peer(request)
    except PermissionError as e:
        return error(401, str(e))
    task_id = request.path_params["task_id"]
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return error(404, "task not found")
    if peer_id not in {row["from_peer"], row["to_peer"]}:
        return error(403, "task is not visible to this peer")
    return JSONResponse({"task": task_to_a2a(row, include_history=True)}, media_type="application/a2a+json")


def stream_event_payload(row: sqlite3.Row) -> dict[str, Any]:
    kind = row["kind"]
    state = row["state"]
    if kind == "artifact":
        artifact = json.loads(row["artifact_json"] or "{}")
        return {
            "artifactUpdate": {
                "taskId": row["task_id"],
                "contextId": row["context_id"],
                "artifact": artifact,
                "lastChunk": True,
            }
        }
    return {
        "statusUpdate": {
            "taskId": row["task_id"],
            "contextId": row["context_id"],
            "status": {
                "state": state or "TASK_STATE_WORKING",
                "timestamp": row["created_at"],
                "message": {
                    "role": row["role"],
                    "parts": [{"text": row["text"] or "", "mediaType": "text/plain"}],
                    "messageId": f"event-{row['id']}",
                    "taskId": row["task_id"],
                    "contextId": row["context_id"],
                },
            },
        }
    }


async def subscribe_task(request: Request) -> Response:
    try:
        peer_id = authenticate_peer(request)
    except PermissionError as e:
        return error(401, str(e))
    task_id = request.path_params["task_id"]
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return error(404, "task not found")
    if peer_id not in {row["from_peer"], row["to_peer"]}:
        return error(403, "task is not visible to this peer")

    async def generate() -> Iterable[bytes]:
        last_id = 0
        deadline = time.time() + 120
        while time.time() < deadline:
            with db() as conn:
                events = conn.execute(
                    "SELECT * FROM events WHERE task_id = ? AND id > ? ORDER BY id",
                    (task_id, last_id),
                ).fetchall()
                task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            for event in events:
                last_id = event["id"]
                yield f"data: {json.dumps(stream_event_payload(event), ensure_ascii=False)}\n\n".encode()
            if task and task["state"] in TERMINAL_STATES and events:
                return
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")


async def agent_inbox(request: Request) -> Response:
    try:
        peer_id = authenticate_peer(request)
    except PermissionError as e:
        return error(401, str(e))
    include_active = request.query_params.get("include_active") == "true"
    states = tuple(ACTIVE_STATES if include_active else {"TASK_STATE_SUBMITTED", "TASK_STATE_INPUT_REQUIRED"})
    placeholders = ",".join("?" for _ in states)
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM tasks
            WHERE to_peer = ? AND state IN ({placeholders})
            ORDER BY updated_at ASC
            LIMIT 50
            """,
            (peer_id, *states),
        ).fetchall()
    return JSONResponse({"tasks": [task_to_a2a(row, include_history=True) for row in rows]})


async def agent_event(request: Request) -> Response:
    try:
        peer_id = authenticate_peer(request)
    except PermissionError as e:
        return error(401, str(e))
    task_id = request.path_params["task_id"]
    try:
        body = await request.json()
    except Exception:
        return error(400, "invalid json")
    state = str(body.get("state") or "TASK_STATE_WORKING")
    if state not in VALID_STATES:
        return error(400, "invalid task state")
    text = str(body.get("text") or "").strip()
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        return error(413, "text too large")
    kind = "artifact" if state == "TASK_STATE_COMPLETED" else "message"
    ts = now_iso()
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return error(404, "task not found")
        if row["to_peer"] != peer_id:
            return error(403, "only the target peer can update this task")
        output_text = text if state == "TASK_STATE_COMPLETED" else row["output_text"]
        completed_at = ts if state in TERMINAL_STATES else row["completed_at"]
        claimed_at = row["claimed_at"] or ts
        conn.execute(
            """
            UPDATE tasks
            SET state = ?, output_text = ?, updated_at = ?, claimed_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (state, output_text, ts, claimed_at, completed_at, task_id),
        )
        artifact = None
        if kind == "artifact":
            artifact = {
                "artifactId": f"{task_id}:response",
                "name": "response",
                "parts": [{"text": text, "mediaType": "text/plain"}],
            }
        add_event(
            conn,
            task_id=task_id,
            context_id=row["context_id"],
            actor_peer=peer_id,
            role="ROLE_AGENT",
            kind=kind,
            text=text,
            state=state,
            artifact=artifact,
        )
        updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return JSONResponse({"task": task_to_a2a(updated, include_history=True)})


async def list_conversations(request: Request) -> Response:
    try:
        require_admin(request)
    except PermissionError as e:
        return error(401, str(e))
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
              context_id,
              COALESCE(topic, context_id) AS topic,
              COUNT(*) AS task_count,
              MIN(created_at) AS created_at,
              MAX(updated_at) AS updated_at,
              GROUP_CONCAT(DISTINCT from_peer) AS senders,
              GROUP_CONCAT(DISTINCT to_peer) AS targets
            FROM tasks
            GROUP BY context_id, COALESCE(topic, context_id)
            ORDER BY updated_at DESC
            LIMIT 100
            """
        ).fetchall()
    return JSONResponse(
        {
            "conversations": [
                {
                    "contextId": r["context_id"],
                    "topic": r["topic"],
                    "taskCount": r["task_count"],
                    "createdAt": r["created_at"],
                    "updatedAt": r["updated_at"],
                    "senders": (r["senders"] or "").split(",") if r["senders"] else [],
                    "targets": (r["targets"] or "").split(",") if r["targets"] else [],
                }
                for r in rows
            ]
        }
    )


async def conversation_detail(request: Request) -> Response:
    try:
        require_admin(request)
    except PermissionError as e:
        return error(401, str(e))
    context_id = request.path_params["context_id"]
    with db() as conn:
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE context_id = ? ORDER BY created_at",
            (context_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM events WHERE context_id = ? ORDER BY id",
            (context_id,),
        ).fetchall()
    return JSONResponse(
        {
            "tasks": [task_to_a2a(row, include_history=True) for row in tasks],
            "events": [
                {
                    "id": row["id"],
                    "taskId": row["task_id"],
                    "contextId": row["context_id"],
                    "actorPeer": row["actor_peer"],
                    "role": row["role"],
                    "kind": row["kind"],
                    "state": row["state"],
                    "text": row["text"],
                    "artifact": json.loads(row["artifact_json"]) if row["artifact_json"] else None,
                    "createdAt": row["created_at"],
                }
                for row in events
            ],
        }
    )


async def admin_peers(request: Request) -> Response:
    try:
        require_admin(request)
    except PermissionError as e:
        return error(401, str(e))
    with db() as conn:
        seen = {row["id"]: row["last_seen"] for row in conn.execute("SELECT * FROM peers")}
    return JSONResponse(
        {
            "peers": [
                {
                    "id": peer_id,
                    "lastSeen": seen.get(peer_id),
                    "agentCardPath": f"/agents/{peer_id}/.well-known/agent-card.json",
                }
                for peer_id in sorted(PEERS)
            ]
        }
    )


async def index(request: Request) -> Response:
    token = html.escape(request.query_params.get("token") or "")
    return HTMLResponse(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>A2A Relay</title>
  <style>
    body {{ margin:0; font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif; background:#0f1115; color:#e8eaed; }}
    header {{ display:flex; gap:12px; align-items:center; padding:12px 16px; border-bottom:1px solid #2a2f3a; background:#171a21; position:sticky; top:0; }}
    input,button {{ font:inherit; border-radius:6px; border:1px solid #3a4150; background:#10131a; color:#e8eaed; padding:7px 9px; }}
    button {{ cursor:pointer; background:#243044; }}
    main {{ display:grid; grid-template-columns:minmax(260px,360px) 1fr; min-height:calc(100vh - 58px); }}
    aside {{ border-right:1px solid #2a2f3a; padding:12px; overflow:auto; }}
    section {{ padding:16px; overflow:auto; }}
    .conv {{ display:block; width:100%; text-align:left; margin:0 0 8px; }}
    .meta {{ color:#9aa4b2; font-size:12px; }}
    .event {{ border:1px solid #2a2f3a; border-radius:8px; padding:10px; margin:0 0 10px; background:#151922; }}
    .event.agent {{ border-left:4px solid #58a6ff; }}
    .event.user {{ border-left:4px solid #7ee787; }}
    pre {{ white-space:pre-wrap; word-break:break-word; margin:8px 0 0; }}
    code {{ color:#9aa4b2; }}
  </style>
</head>
<body>
<header>
  <strong>A2A Relay</strong>
  <input id="token" type="password" placeholder="admin token" value="{token}">
  <button onclick="save()">Save</button>
  <button onclick="load()">Refresh</button>
  <span id="status" class="meta"></span>
</header>
<main>
  <aside>
    <h3>Conversations</h3>
    <div id="conversations"></div>
    <h3>Peers</h3>
    <div id="peers" class="meta"></div>
  </aside>
  <section>
    <h2 id="title">Select a conversation</h2>
    <div id="events"></div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
if (!{json.dumps(bool(token))}) $('token').value = localStorage.a2aRelayToken || '';
function headers() {{ return {{'X-Admin-Token': $('token').value}}; }}
function save() {{ localStorage.a2aRelayToken = $('token').value; load(); }}
async function api(path) {{
  const r = await fetch(path, {{headers: headers()}});
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}}
async function load() {{
  try {{
    const st = await fetch('/api/status').then(r => r.json());
    $('status').textContent = `${{st.peerCount}} peers · ${{st.taskCount}} tasks · ${{st.eventCount}} events`;
    const peers = await api('/api/admin/peers');
    $('peers').innerHTML = peers.peers.map(p => `<div><code>${{p.id}}</code><br>${{p.agentCardPath}}<br>last seen: ${{p.lastSeen || 'never'}}</div>`).join('<hr>');
    const data = await api('/api/conversations');
    $('conversations').innerHTML = data.conversations.map(c =>
      `<button class="conv" onclick="openConv('${{c.contextId}}')"><strong>${{esc(c.topic)}}</strong><div class="meta">${{c.taskCount}} task(s) · ${{esc([...c.senders,...c.targets].join(' ↔ '))}}<br>${{c.updatedAt}}</div></button>`
    ).join('');
  }} catch (e) {{ $('status').textContent = e.message; }}
}}
async function openConv(id) {{
  const data = await api('/api/conversations/' + encodeURIComponent(id));
  $('title').textContent = id;
  $('events').innerHTML = data.events.map(e => {{
    const cls = e.role === 'ROLE_AGENT' ? 'agent' : 'user';
    const body = e.text || (e.artifact ? JSON.stringify(e.artifact, null, 2) : '');
    return `<div class="event ${{cls}}"><div><strong>${{esc(e.actorPeer)}}</strong> <span class="meta">${{esc(e.state || e.kind)}} · ${{e.createdAt}}</span></div><pre>${{esc(body)}}</pre></div>`;
  }}).join('');
}}
function esc(s) {{ return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
load();
setInterval(load, 5000);
</script>
</body>
</html>"""
    )


routes = [
    Route("/", index, methods=["GET"]),
    Route("/api/status", status, methods=["GET"]),
    Route("/api/admin/peers", admin_peers, methods=["GET"]),
    Route("/api/conversations", list_conversations, methods=["GET"]),
    Route("/api/conversations/{context_id}", conversation_detail, methods=["GET"]),
    Route("/api/agent/inbox", agent_inbox, methods=["GET"]),
    Route("/api/agent/tasks/{task_id}/events", agent_event, methods=["POST"]),
    Route("/.well-known/agent-card.json", catalog_card, methods=["GET"]),
    Route("/agents/{peer_id}/.well-known/agent-card.json", agent_card, methods=["GET"]),
    Route("/agents/{peer_id}/message:send", send_message, methods=["POST"]),
    Route("/agents/{peer_id}/tasks/{task_id}", get_task, methods=["GET"]),
    Route("/agents/{peer_id}/tasks/{task_id}:subscribe", subscribe_task, methods=["POST"]),
]


def create_app() -> Starlette:
    ensure_dirs()
    bootstrap_peers()
    reload_peers()
    init_db()
    return Starlette(routes=routes)


app = create_app()


if __name__ == "__main__":
    if not ADMIN_TOKEN:
        print("a2a-bridge: WARN BRIDGE_ADMIN_TOKEN is not set; admin UI/API are open", file=sys.stderr)
    uvicorn.run(app, host=HOST, port=PORT)
