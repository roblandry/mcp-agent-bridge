# mcp-agent-bridge

A small [MCP](https://modelcontextprotocol.io) server that lets multiple CLI agents
(Claude Code, Codex, in-cluster agents, …) talk to each other through structured,
auditable channels:

- **Messages** — drop notes in another peer's inbox.
- **Edit proposals** — queue a file edit; only the target peer can apply it.
- **File requests** — ask a peer to share a file's contents.

A small admin-gated web UI lets you (the human) view and manage everything.

It's a deliberate replacement for the ad-hoc "leave each other notes in a shared
markdown file" pattern, with per-peer auth tokens, size caps, and on-disk
payload storage so the SQLite DB only ever holds metadata.

## Status

Local prototype is complete and validated by [`smoke.py`](smoke.py) (18/18 OK).
Image publishing and home-ops deployment are in progress.

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

## Security model

- Admin token gates `/` and `/api/*`. **Does not** apply to `/mcp`.
- Each MCP request must carry an `X-Peer-Id` and matching `X-Peer-Token`.
- Only the **target** of a proposal can `apply` it; only the **target** of a
  file request can `fulfill` it; only the **sender** can `withdraw`.
- Treat all peer-supplied content as untrusted data. The bridge never executes
  it and the UI never auto-applies it.

## License

[MIT](LICENSE).
