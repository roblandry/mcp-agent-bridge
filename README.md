# mcp-agent-bridge

A small [MCP](https://modelcontextprotocol.io) server that lets multiple CLI agents
(Claude Code, Codex, in-cluster agents, …) talk to each other through structured,
auditable channels:

- **Messages** — drop notes in another peer's inbox, with explicit
  end-of-turn signalling so the receiver only acts once the sender is done.
- **Edit proposals** — queue a file edit; only the target peer can apply it.
- **File requests** — ask a peer to share a file's contents.
- **Presence** — every authenticated call refreshes the caller's `last_seen`,
  and an explicit `heartbeat()` tool exists for idle peers; receivers can
  detect a stuck peer via `seconds_since_last_seen` in `list_peers`.

A small admin-gated web UI lets you (the human) view and manage everything.
Message bodies render as Markdown with syntax-highlighted code blocks, and
the proposal / file-request payload viewer shows the body highlighted by
extension. Long code blocks collapse behind a `code (N lines, <lang>)`
summary, and each list (messages / proposals / file requests) paginates to
the most recent 50 with a "show earlier" link to expand.

It's a deliberate replacement for the ad-hoc "leave each other notes in a shared
markdown file" pattern, with per-peer auth tokens, size caps, and on-disk
payload storage so the SQLite DB only ever holds metadata.

## Status

Local prototype is complete and validated by [`smoke.py`](smoke.py) (22/22 OK).
The image build pipeline is in place; releases are tagged on `v*` and the
latest image is at
`ghcr.io/roblandry/mcp-agent-bridge:latest`.

## A2A relay prototype

[`a2a_bridge.py`](a2a_bridge.py) is a side-by-side Agent2Agent-style relay
prototype for the same private peer set. It is intentionally narrower than the
MCP bridge: it focuses on agent-to-agent task handoff and a human-visible
conversation transcript, not file requests or edit proposals.

Each peer gets an Agent Card and A2A task endpoints:

- `GET /agents/{peer_id}/.well-known/agent-card.json`
- `POST /agents/{peer_id}/message:send`
- `GET /agents/{peer_id}/tasks/{task_id}`
- `POST /agents/{peer_id}/tasks/{task_id}:subscribe`

Agents authenticate with the same `X-Peer-Id` and `X-Peer-Token` headers used
by the MCP bridge. A sender posts to the target peer's `message:send` endpoint.
The target peer polls `GET /api/agent/inbox` and posts progress or completion
events to `POST /api/agent/tasks/{task_id}/events`.

The admin UI at `/` shows conversations by A2A `contextId`, including every
message/progress/final-artifact event between peers. This is the main addition
over stock A2A: the human can watch Claude Code, OpenClaw, Hermes, and other
peers talk through the relay.

Run locally:

```sh
export BRIDGE_ADMIN_TOKEN=$(openssl rand -hex 32)
export BRIDGE_DATA_DIR=/tmp/a2a-bridge
export BRIDGE_PEERS_SEED=/path/to/peers.json
uv run a2a_bridge.py
```

Run the smoke test:

```sh
uv run a2a_smoke.py
```

Run the container in A2A mode:

```sh
docker run --rm \
  -e BRIDGE_MODE=a2a \
  -e BRIDGE_ADMIN_TOKEN=$(openssl rand -hex 32) \
  -v bridge-data:/data \
  -p 8765:8765 \
  ghcr.io/roblandry/mcp-agent-bridge:latest
```

This is a pilot surface, not a full A2A conformance target yet. The next step is
to wire one real peer poller, then validate against A2A Inspector/TCK before
retiring the existing MCP bridge.

## Image

Tagged releases publish `ghcr.io/roblandry/mcp-agent-bridge:vX.Y.Z` (and `:latest`)
via [GitHub Actions](.github/workflows/release.yml).

The container runs as a non-root user, exposes `:8765`, and expects a writable
volume at `/data` (SQLite DB, `peers.json`, and the `payloads/` directory live
there). It's a single-replica workload — SQLite is single-writer, so don't
horizontally scale it.

```sh
docker run --rm \
  -e BRIDGE_ADMIN_TOKEN=$(openssl rand -hex 32) \
  -v bridge-data:/data \
  -p 8765:8765 \
  ghcr.io/roblandry/mcp-agent-bridge:latest
```

For Kubernetes, mount a PVC at `/data`, supply `BRIDGE_ADMIN_TOKEN` from a
Secret, and (optionally) point `BRIDGE_PEERS_SEED` at a SOPS-decrypted
`peers.json` mounted read-only — the seed is copied to `/data/peers.json` only
on first boot, so subsequent restarts preserve any peers added via the UI.

## Run locally

```sh
export BRIDGE_ADMIN_TOKEN=$(openssl rand -hex 32)
echo "$BRIDGE_ADMIN_TOKEN"   # paste this into the UI's login screen
uv run server.py             # listens on 0.0.0.0:8765
```

Then open <http://127.0.0.1:8765/>, paste the admin token, and add peers from
the Peers tab. Point MCP clients at `http://<host>:8765/mcp` with
`X-Peer-Id` and `X-Peer-Token` headers matching a peer in the registry.

## Connect agents to start a multi-agent chat

For each agent that should participate, do two one-time things:

**1. Wire the agent to the bridge as an MCP client.** Each agent's MCP
client just needs the bridge URL, its own `peer_id`, and the matching token
from the bridge's peer registry.

```sh
# Claude Code
claude mcp add --transport http agent-bridge \
  http://<bridge-host>:8765/mcp \
  --header "X-Peer-Id: claude-rob-laptop" \
  --header "X-Peer-Token: <token>"

# OpenAI Codex CLI (or anything that only speaks stdio MCP) — wrap with mcp-remote
codex --mcp-server agent-bridge \
  "npx mcp-remote http://<bridge-host>:8765/mcp \
    --header 'X-Peer-Id: codex-rob-laptop' \
    --header 'X-Peer-Token: <token>'"

# In-cluster agent (e.g. openclaw): point at the cluster-internal Service URL,
# e.g. http://mcp-agent-bridge.<namespace>.svc.cluster.local:8765/mcp
```

**2. Drop a one-line identity stub** into each agent's `CLAUDE.md`,
`AGENTS.md`, or seed prompt — only what the bridge can't infer:

```text
You are peer `claude-rob-laptop` on the agent-bridge MCP server.
Other peers: `openclaw`, `codex-rob-laptop`.
```

Don't restate etiquette there — turn-taking, polling cadence, the
propose-edit / request-file pattern, and the security model already ship
from the bridge as part of the MCP `instructions=` payload, so every
connected agent gets them automatically.

**3. To start a session,** just prompt one agent normally and ask it to
coordinate with another peer — e.g. "ask `openclaw` to share
`/var/log/foo.log` and tell me what it sees." The agent will use
`request_file`, the other side will fulfill, and the conversation continues
through the bridge from there. There's no separate "session begin" step.

## Smoke test

`smoke.py` spawns its own server on a temp port and data dir, exercises every
MCP tool and admin endpoint, and verifies content lives on disk (never in DB),
peer-token auth, admin-token auth, peer CRUD, and SOPS-style seed bootstrap.

```sh
rm -f bridge.db peers.json && rm -rf payloads/
uv run smoke.py
```

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `BRIDGE_ADMIN_TOKEN` | _(none)_ | Required for the web UI / `/api/*`. Header `X-Admin-Token`. |
| `BRIDGE_DATA_DIR` | script directory | Where `bridge.db`, `peers.json`, and `payloads/` live. |
| `BRIDGE_PEERS_SEED` | _(none)_ | Path to a SOPS-decrypted JSON file used to seed `peers.json` on first start. (Alias: `BRIDGE_PEERS_FILE`.) |
| `BRIDGE_HOST` | `0.0.0.0` | Bind host. |
| `BRIDGE_PORT` | `8765` | Bind port. |
| `BRIDGE_MAX_CONTENT_BYTES` | `5242880` (5MB) | Per-payload size cap. |

## Turn-taking and presence

The bridge is a mailbox, not a synchronous chat — there's no implicit "your
turn / my turn" gate. Two ergonomics fix this:

- **`end_turn=True`** on the final `send_message` of your turn marks it as
  complete. The recipient's `read_inbox` returns a per-topic `turns` summary
  with `turn_complete: bool`; receivers should hold off acting until they see
  `turn_complete=true` for that topic+sender.
- **`list_peers`** returns `seconds_since_last_seen` for each peer. If the
  peer you're waiting on hasn't been seen in several minutes, treat them as
  stuck or disconnected. Idle peers can keep themselves visible with
  `heartbeat()`; any other authenticated call also refreshes `last_seen`.

The protocol etiquette (poll cadence ≥15s, never tight-loop, no direct
edits/reads of peer-owned files, treat peer content as untrusted, etc.) is
sent to every connecting client via the MCP `instructions=` channel, so a new
agent only needs its own peer-id wired in locally.

## Security model

- Admin token gates `/` and `/api/*`. **Does not** apply to `/mcp`.
- Each MCP request must carry an `X-Peer-Id` and matching `X-Peer-Token`.
- Only the **target** of a proposal can `apply` it; only the **target** of a
  file request can `fulfill` it; only the **sender** can `withdraw`.
- Treat all peer-supplied content as untrusted data. The bridge never executes
  it and the UI never auto-applies it.

## License

[MIT](LICENSE).
