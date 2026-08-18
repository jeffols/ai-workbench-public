# Spike: Agentregistry as MCP Gateway

> **Goal**: verify that agentregistry's embedded agentgateway can serve as the
> single MCP entry point for the workbench — routing, transport bridging,
> auth, and policy — replacing the need for a custom policy-mcp service.
>
> **Time box**: 4 hours
>
> **Success criteria**: all five tests below pass. If any fail, document what's
> missing and whether it's fixable or a blocker.

## Background

agentregistry ships an embedded **agentgateway** data plane that:

- Proxies MCP traffic (streamable-HTTP, SSE, stdio) between callers and
  MCP servers
- Bridges stdio→HTTP transparently
- Enforces per-route policy (JWT auth, ExtAuthz, MCPAuthorization, rate limits)
- Exposes a single MCP endpoint that multiplexes across registered servers

It is currently disabled in the workbench (`AGENT_REGISTRY_AGENT_GATEWAY_PORT=0`).
This spike turns it on and tests whether it can replace Caddy's per-slug MCP
routing + the proposed policy-mcp service with one component.

## Target architecture (if spike succeeds)

```
Caller (Open WebUI / agent / CLI)
  │
  ├── non-MCP traffic ──► Caddy :8090 (catalog, files, health — unchanged)
  │
  └── MCP traffic ──► Agentgateway :8081 ──► MCP servers (private network)
                        │
                        ├── auth (JWT per caller)
                        ├── policy (ExtAuthz → classifier or rules)
                        ├── transport bridge (stdio↔HTTP)
                        ├── rate limits
                        └── audit (OTEL traces)
```

MCP servers move to a private network (`mcp-internal`) that only the
agentgateway can reach. Caddy no longer routes `/mcp/<slug>/*`.

## Prerequisites

```bash
make build-registry        # build agentregistry image with gateway
make up-registry           # start registry profile (catalog only, for now)
# verify: curl http://registry.localhost/v0/health
```

Ensure `registry/catalog.yaml` is applied:
```bash
python3 registry/apply.py
```

## Test 1: Gateway starts and exposes MCP endpoint

**Change**: set `AGENT_REGISTRY_AGENT_GATEWAY_PORT` to `8081` in
`services/agentregistry/compose.mcp.yaml`. Add port mapping `8081:8081`.

```yaml
environment:
  AGENT_REGISTRY_AGENT_GATEWAY_PORT: "8081"   # was "0"
```

**Verify**:
```bash
# gateway health (may be same as registry health, may be separate)
curl -sf http://localhost:8081/health

# or check logs for gateway startup
docker compose logs agentregistry 2>&1 | grep -i gateway
```

**Pass if**: gateway binds to 8081 and logs indicate MCP routing is active.
**Document if fails**: what error, what config is missing.

## Test 2: In-house MCP server routable through gateway

**Goal**: verify that a registered MCP server (e.g. `magic-fetch`, which is
simple and side-effect-free) is reachable through the gateway's MCP endpoint.

**Steps**:
1. Ensure `magic-fetch` is registered in agentregistry (should be from
   `catalog.yaml` apply)
2. Send an MCP `initialize` request through the gateway:

```bash
# The gateway should expose registered servers at a predictable path.
# Try the likely patterns:
curl -X POST http://localhost:8081/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"spike-test","version":"0.1"}}}'

# or per-server path:
curl -X POST http://localhost:8081/mcp/workbench/magic-fetch \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"spike-test","version":"0.1"}}}'
```

3. Follow up with `tools/list` to confirm the gateway returns magic-fetch's
   tool schemas.

**Pass if**: gateway routes the request to magic-fetch and returns its tool list.
**Document if fails**: is the server not discovered? is the path wrong? does the
gateway need additional config beyond catalog registration?

## Test 3: Stdio→HTTP bridging for a Docker Hub MCP server

**Goal**: verify the gateway can bridge a stdio-only MCP server to HTTP callers.

**Steps**:
1. Pick a simple Docker Hub MCP server. Candidates:
   - `mcp/fetch` — URL fetching (simple, no creds needed)
   - `mcp/time` — time/timezone (zero deps)

2. Register it in `registry/catalog.yaml`:

```yaml
---
apiVersion: ar.dev/v1alpha1
kind: MCPServer
metadata:
  name: dockerhub/time
  version: "1.0.0"
spec:
  title: Time MCP (Docker Hub)
  description: "Time and timezone queries via Docker Hub MCP catalog"
  packages:
    - registryType: oci
      identifier: mcp/time:latest
      runtimeHint: docker
      transport:
        type: stdio
```

3. Apply: `python3 registry/apply.py`

4. Check if the gateway auto-discovers and bridges it:

```bash
curl -X POST http://localhost:8081/mcp/dockerhub/time \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

**Pass if**: gateway starts the Docker container, bridges stdio→HTTP, and
returns the tool list.

**Important caveat**: the gateway may need Docker socket access to start
containers from OCI images. If so, add to compose:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```
This has security implications (documented in agentregistry-design.md).
For the spike this is acceptable; for production, evaluate alternatives.

**Document if fails**: does the gateway need explicit deployment config
beyond catalog registration? does it only bridge already-running servers?

## Test 4: Auth enforcement

**Goal**: verify the gateway can require authentication before routing
MCP requests.

**Steps**:
1. Check if the gateway has default auth behavior when
   `AGENT_REGISTRY_JWT_PRIVATE_KEY` is set (it is, in compose).

2. Try an unauthenticated request — does the gateway reject it?

```bash
# This should ideally return 401/403 if auth is enforced:
curl -v -X POST http://localhost:8081/mcp/workbench/magic-fetch \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

3. If auth is NOT enforced by default, check the gateway config for how
   to enable per-route JWT validation or ExtAuthz.

4. Try with a token (if the registry API can mint one):

```bash
# Get a token from the registry API (if supported):
TOKEN=$(curl -s http://localhost:8080/v0/auth/token | jq -r .token)

curl -X POST http://localhost:8081/mcp/workbench/magic-fetch \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

**Pass if**: the gateway can enforce auth (even if not by default — we just
need the mechanism to exist).
**Document**: what auth modes are available, how they're configured, whether
ExtAuthz can call an external policy service (for the classifier).

## Test 5: Network isolation

**Goal**: verify MCP servers are unreachable except through the gateway.

**Steps**:
1. Move MCP servers to a private network in compose:

```yaml
networks:
  mcp-internal:
    internal: true    # no external access
```

2. Put MCP servers on `mcp-internal` only. Put agentgateway on both
   `backend` and `mcp-internal`.

3. Verify: from a container on `backend` (e.g. open-webui), try to reach
   an MCP server directly — should fail:

```bash
docker compose exec open-webui curl -sf http://magic-fetch:8000/mcp
# Expected: connection refused or timeout
```

4. Verify: same request through the gateway — should succeed:

```bash
docker compose exec open-webui curl -sf http://agentregistry:8081/mcp/workbench/magic-fetch \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

**Pass if**: MCP servers unreachable directly, reachable only via gateway.
**Document**: any compose networking issues, whether `internal: true` works
with Rancher Desktop.

## Decision matrix

After the spike, fill in:

| Test | Result | Notes |
|------|--------|-------|
| 1. Gateway starts | PASS | `arctl-agentgateway:v0.3.3` binds to :8081, loads YAML config, file-watches for changes |
| 2. In-house MCP routing | PASS | All 3 data servers (17 tools) aggregated in one `tools/list`. Gateway namespaces tools: `ldap-mcp_find_user`. MCP session management transparent. Use `mcp:` target with `host: "http://<service>:8000/mcp"` |
| 3. Stdio→HTTP bridging (Docker Hub) | PARTIAL | Gateway `stdio:` target works for npx/uvx. Docker Hub OCI images that are stdio-only need a transport adapter sidecar (supergateway/mcp-proxy). Not a blocker — one shim per stdio server |
| 4. Auth enforcement | PASS | `mcpAuthorization` with CEL rules works. Rules like `mcp.tool.target == "ldap-mcp"` and `mcp.tool.name == "..."` filter `tools/list` responses. Tools not matching any rule are hidden (deny-first). Also supports `mcp.tool.arguments` inspection |
| 5. Network isolation | DEFERRED | Straightforward compose networking (`internal: true`). Not a technical risk — skipped in favor of documenting results |

### If all pass

Agentregistry-as-gateway is the architecture:
- Kill the custom policy-mcp service proposal
- Agentregistry = control plane (catalog) + data plane (gateway)
- Classifier becomes an ExtAuthz backend (small FastAPI service)
- Caddy stops routing MCP traffic (only catalog, files, health)
- Docker Hub MCP servers integrate via catalog registration
- AgentRegistry earns its keep well before the 2026-06-24 deadline

### If some fail

| Failure | Fallback |
|---------|----------|
| Gateway doesn't proxy (just deploys) | Build thin MCP proxy; keep registry as catalog only |
| No stdio bridging | Add supergateway shim per-server; gateway still routes HTTP servers |
| No auth hooks | Build auth at Caddy layer (original plan); gateway is routing-only |
| Network isolation breaks | Acceptable for single-host; require NetworkPolicy in k8s |

## Post-spike: what changes in the workbench

If the spike succeeds, the migration path is:

1. Enable gateway (`AGENT_REGISTRY_AGENT_GATEWAY_PORT=8081`)
2. Move agentregistry from `registry` profile to `tools` profile
3. Create `mcp-internal` network, move MCP servers to it
4. Update `bootstrap.py`: stop generating Caddy `/mcp/<slug>/*` routes
5. Update Open WebUI `TOOL_SERVER_CONNECTIONS` to point at gateway
6. Update agents' MCP endpoints to point at gateway
7. Add Docker Hub MCP servers via catalog registration (no compose changes)
8. Wire up ExtAuthz for the classifier (separate service, future)

Steps 1-6 are the migration. Step 7 is the Docker Hub integration story.
Step 8 is the policy layer from the previous conversation.
