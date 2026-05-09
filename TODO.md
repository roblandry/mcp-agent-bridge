# TODO

Working list of what's left, in roughly the order to tackle it. Architecture and
locked-in decisions live in [CLAUDE.md](CLAUDE.md); this file is just the
roadmap.

## Recently done

- **Repo published** at <https://github.com/roblandry/mcp-agent-bridge>;
  `main` pushed (latest commit `4d35ef9`).
- **Image build pipeline**: `Dockerfile`, `.dockerignore`,
  `.github/workflows/release.yml` (tag-triggered GHCR push, `linux/amd64`
  only), `README.md`, `LICENSE` (MIT). Local `docker build` succeeds; the
  container starts and `/api/status` responds. Final image ~202MB — the
  earlier "<100MB" target was unrealistic given fastmcp's deps
  (pydantic + cryptography + mcp + uvicorn). Not pursuing Alpine.
- **Turn-taking + presence**: `messages.end_turn`, `read_inbox` returns
  `{messages, turns}`, `list_peers` exposes `seconds_since_last_seen`,
  `heartbeat()` tool. `BRIDGE_INSTRUCTIONS` rewritten to teach the protocol.
  Smoke now 22/22.
- **`FastMCP(instructions=...)` wired in** — protocol etiquette ships from
  the server, every client gets it on connect.
- **Strict pyright + ruff clean**. `pyrightconfig.json` set to strict;
  local `.venv` (gitignored) so Pylance can resolve imports. Real bugs
  surfaced and fixed: `ADMIN_TOKEN.encode()` Optional guard, `cur.lastrowid`
  narrowing via `_new_id()`.

## 1. Cut the first release

When ready, tag and push:

- [ ] `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z` to fire
  `.github/workflows/release.yml` and produce
  `ghcr.io/roblandry/mcp-agent-bridge:vX.Y.Z` + `:latest`.
- [ ] After the first push, flip the GHCR package to public:
  `gh api -X PATCH /user/packages/container/mcp-agent-bridge --field visibility=public`
  (or via the GHCR UI). Otherwise the cluster needs a pull secret.

## 2. Finish the home-ops manifests

Working tree at `~/Nextcloud/Projects/home-ops/kubernetes/apps/ai/mcp-agent-bridge/`.
Most of the manifests are in good shape (validated 2026-05-09); only a few
gaps remain:

- [ ] **`app/secret.sops.yaml`** — namespace and SOPS shell are correct, but
  `stringData` is still `null`. Decrypt with `sops`, add a `peers.json` entry
  containing the initial peer→token JSON, re-encrypt, commit. Suggested peer
  ids: `claude-rob-laptop`, `openclaw`, `codex-rob-laptop`. Generate tokens
  with `openssl rand -hex 32` per peer.
- [ ] **Image tag alignment** — `helmrelease.yaml` currently pins
  `tag: v0.1.0`. Don't deploy until that tag actually exists in GHCR (see
  item #1) — until then Flux will sit in `ImagePullBackOff`.
- [ ] **Validate**: `python3 scripts/check-schemas.py` and
  `task flux-local:test` (the second needs Docker, mirrors CI).

## 3. Wire the agents

After the deployment is up and you have the admin token
(`kubectl -n ai get secret mcp-agent-bridge-admin-token -o jsonpath='{.data.token}' | base64 -d`):

- **openclaw** (in-cluster): point its MCP HTTP config at
  `http://mcp-agent-bridge.ai.svc.cluster.local:8765/mcp`. Headers
  `X-Peer-Id: openclaw`, `X-Peer-Token: <openclaw's token from peers seed>`.
  Goes via cluster DNS, not ingress — no TLS overhead.
- **Claude Code** (laptop):
  `claude mcp add --transport http agent-bridge https://bridge.${SECRET_DOMAIN}/mcp --header "X-Peer-Id: rob-laptop" --header "X-Peer-Token: <token>"`.
- **Codex** (laptop): if the Codex version doesn't natively do HTTP MCP, wrap
  with
  `npx mcp-remote https://bridge.${SECRET_DOMAIN}/mcp --header "X-Peer-Id: rob-codex" --header "X-Peer-Token: <token>"`
  configured as a stdio command in Codex's `mcp_servers` config.

## 4. Per-agent identity stubs

The protocol etiquette already ships from the server via
`BRIDGE_INSTRUCTIONS`. The only thing each agent still needs locally is
**who it is** — that's part of step #3 above (the `X-Peer-Id` header in
its MCP client config), plus a one-line stub in its own
CLAUDE.md/AGENTS.md/seed prompt:

> *"You are peer `rob-laptop`. Other peers: `openclaw`, `rob-codex`."*

Don't restate the etiquette there — it's already in the server's
`instructions`.

Optional, nice-to-have:

- [ ] Expose a `@mcp.prompt` named `bridge_status` or `bridge_onboard` for
  clients that surface MCP prompts as slash commands. Not required for
  v0.1.0.
