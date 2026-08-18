# Session checkpoint — 2026-04-26 morning

## What happened this session

Started from the "Dive into Claude Code" paper. Read it, compared it to
ChatGPT's analysis turns, calibrated against the current workbench state.
That led to four design constraints (saved to memory) and then a concrete
spike on agentgateway.

## Where you are right now

**Branch**: `spike/agentgateway-proxy` (7 commits ahead of main)

**What's running**: 21 containers — full workbench + Traefik + agentregistry +
agentgateway + mcp-time (Docker Hub `mcp/time` bridged via supergateway).

**What's merged**: the `network-isolation-traefik` branch (agents/tools/proxy
network split + Traefik shared ingress) is merged into the spike branch.

## What works

- **Agentgateway** proxies MCP traffic, aggregates tools from multiple
  backends, manages sessions. Validated from curl with magic-fetch + mcp-time.
- **CEL-based mcpAuthorization** filters tool visibility (deny-first). Tested:
  rules can allow/deny by tool name or target server.
- **mcp-time** (Docker Hub stdio server) bridged via supergateway
  (stdio→streamable-HTTP) and routed through both the gateway and Caddy.
- **Traefik** on :80 routing `*.localhost` hostnames, Caddy behind it.
- **Caddy SSE streaming** fixed (`flush_interval -1`) — was buffering
  text/event-stream and returning empty responses.

## What doesn't work yet

- **Open WebUI → agentgateway**: MCP Python SDK has an async bug
  (`Attempted to exit cancel scope in a different task`) when talking to the
  gateway's multiplexed MCP endpoint. Upstream issue, not fixable in config.
- **Open WebUI → mcp-time via Caddy**: Caddy streaming is now fixed, but
  Open WebUI still reports "Initialized 0 tool server(s)". This is the known
  issue where Open WebUI persists tool-server config in its volume and ignores
  the `TOOL_SERVER_CONNECTIONS` env var on restart. You may need to delete the
  `openwebui-data` volume (loses chat history) or manually add/refresh the
  connection in Settings > Tool Servers.
- **Open WebUI tool server initialization**: "Initialized 0 tool server(s)"
  on every restart. The env var works on fresh volumes only. The existing
  tools (code-agent, enrichment-agent, etc.) work because they were configured
  on the original volume — new ones don't automatically appear.

## What to do next (pick one)

1. **Fix Open WebUI tool discovery**: either delete the volume (`make clean`
   + `make up`), or manually add mcp-time in Settings > Tool Servers with URL
   `http://caddy:8090/mcp/mcp-time/mcp`. This should unblock the end-to-end
   test.

2. **Wire mcp-time through agentregistry's deployment adapter** instead of
   hand-managing it. This was your instinct ("shouldn't mcp-time go through
   agentregistry?") — the deployment API needs `providerId` and `version`
   fields we haven't figured out yet.

3. **Move on to policy layer**: the gateway's CEL rules work. Next step
   would be identity (bearer tokens per agent/user) so the CEL rules can
   reference `jwt.sub` and the policy becomes meaningful.

## Key files changed

- `scripts/bootstrap.py` — gateway tool connection + Caddy flush_interval
- `services/agentregistry/compose.mcp.yaml` — gateway port 8081, agentgateway service
- `services/mcp-time/` — Dockerfile (supergateway + mcp-server-time), compose, manifest
- `config/agentgateway/agent-gateway.yaml` — gateway routing config
- `docker-compose.yml` — mcp-time include + merged network isolation
- `docs/spike-agentgateway-proxy.md` — spike plan + results
- `docs/deep/` — paper + ChatGPT analysis turns

## Commits on spike branch

```
3cbb7f6 fix: add flush_interval to Caddy MCP reverse proxy for SSE streaming
6da21ac route mcp-time through Caddy like other tools
f75f33a add agentgateway as Open WebUI tool connection via bootstrap
13e856c merge network-isolation-traefik + update gateway/mcp-time networks
6db0349 add agentgateway + mcp-time Docker Hub integration
b59a0d8 spike: agentgateway as MCP proxy — validated routing, auth, and tool aggregation
```
