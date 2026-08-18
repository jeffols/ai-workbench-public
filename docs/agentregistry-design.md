# Agentregistry Integration — Design Document

## Problem

The AI Workbench lacks a formal **registry** layer — a discoverable,
versioned catalog of agents, MCP servers, skills, and prompts.  The current
status dashboard (`ai-workbench.localhost`) shows service health dots but
carries no metadata about what each component does, what version it is, or
how it relates to other components.

As the workbench scales toward enterprise adoption, we need a catalog that:

- Documents every agentic capability (what it does, what it depends on)
- Versions artifacts so changes are traceable
- Maps workbench capabilities to enterprise equivalents
- Provides a foundation for governance and curation workflows

## Proposed Solution

Adopt **[Agentregistry](https://github.com/agentregistry-dev/agentregistry)**
(Apache 2.0, CNCF-backed) in **registry-only mode** — catalog and web UI
only, no agentgateway, no deployment orchestration.

### What stays the same

| Component | Role | Change |
|-----------|------|--------|
| Caddy | Reverse proxy, `.localhost` routing | Add `registry.localhost` block |
| LiteLLM | Model gateway | None |
| Open WebUI | Chat frontend / orchestrator | None |
| Phoenix | OTEL tracing | None |
| mcpo | MCP-to-HTTP bridge | None |
| MCP agents | Tool servers | None |

### What's new

| Component | Role | Resources |
|-----------|------|-----------|
| `agentregistry` | Catalog server + web UI | ~256–512 MB RAM |
| `registry-db` | PostgreSQL for registry metadata | ~128 MB RAM |

## Database Strategy

### Decision: Separate Postgres container

The workbench already runs `litellm-db` (`postgres:16-alpine`).  Two options
were evaluated:

| Option | Pros | Cons |
|--------|------|------|
| **A. Share `litellm-db`** | One container, less memory | Requires Alpine→Debian image swap (risky with existing data); operational coupling; init scripts don't run on existing volumes |
| **B. Separate `registry-db`** | Zero risk to existing data; independent lifecycle; can use `pgvector` image from day one | Extra container (~128 MB) |

**Recommendation: Option B** — a separate `registry-db` container.

- `litellm-db` stays untouched (`postgres:16-alpine`)
- `registry-db` uses `pgvector/pgvector:pg16` (Debian-based, ARM64 support)
- pgvector extension available when semantic search is enabled later
- No migration risk to existing LiteLLM data

### Future consolidation

If memory pressure demands it, consolidation into a single Postgres can be
done later via:
1. Backup both databases
2. Replace `litellm-db` image with `pgvector/pgvector:pg16`
3. Start fresh volume, restore both databases
4. Remove `registry-db`

## Architecture

```
Browser
  │
  ├── chat.localhost ──────► Open WebUI ──► LiteLLM ──► OpenAI/Anthropic
  ├── registry.localhost ──► Agentregistry (catalog UI, port 8080)
  ├── phoenix.localhost ───► Phoenix (tracing)
  ├── files.localhost ─────► Caddy → Open Terminal (file browser)
  └── ai-workbench.localhost ► Status dashboard
                                │
                        Agentregistry ──► registry-db (pgvector/pg16)
                        LiteLLM ────────► litellm-db  (postgres:16-alpine)
```

## Registry-Only Mode

Agentregistry ships with an embedded **agentgateway** for MCP routing and
deployment orchestration.  In registry-only mode we disable these:

| Feature | Status | How |
|---------|--------|-----|
| Artifact catalog (browse, search) | ✅ Enabled | Core feature |
| Web UI | ✅ Enabled | Served by agentregistry on :8080 |
| REST API | ✅ Enabled | For `arctl` and automation |
| Semantic search | ⏸ Disabled initially | Enable later via `AGENT_REGISTRY_DATABASE_VECTOR_ENABLED=true` + embeddings config |
| Agentgateway (MCP proxy) | ❌ Disabled | `AGENT_REGISTRY_AGENT_GATEWAY_PORT=0` |
| Local Docker deployments | ❌ Disabled | Don't mount Docker socket |
| IDE auto-configuration | ⏸ Optional | `arctl configure cursor` works from host |

### Known limitation

The agentregistry web UI may still show "Deploy" buttons even with the
gateway disabled.  Clicking them will fail gracefully (no Docker socket
mounted).  This is acceptable for a development workbench.  If it becomes
confusing, we can add a note to the status dashboard.

## Compose Fragment

```yaml
# services/agentregistry/compose.mcp.yaml
services:
  agentregistry:
    image: ${AGENTREGISTRY_IMAGE:-ghcr.io/agentregistry-dev/agentregistry:latest}
    restart: unless-stopped
    profiles: [tools]
    environment:
      AGENT_REGISTRY_SERVER_ADDRESS: ":8080"
      AGENT_REGISTRY_DATABASE_URL: >-
        postgres://agentregistry:${REGISTRY_DB_PASSWORD}@registry-db:5432/agentregistry?sslmode=disable
      AGENT_REGISTRY_AGENT_GATEWAY_PORT: "0"
      AGENT_REGISTRY_DATABASE_VECTOR_ENABLED: "false"
      AGENT_REGISTRY_ENABLE_REGISTRY_VALIDATION: "false"
      AGENT_REGISTRY_JWT_PRIVATE_KEY: ${REGISTRY_JWT_KEY}
    depends_on:
      registry-db: { condition: service_healthy }
    deploy:
      resources:
        limits: { memory: 512M, cpus: "1" }
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8080/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3

  registry-db:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    profiles: [tools]
    environment:
      POSTGRES_DB: agentregistry
      POSTGRES_USER: agentregistry
      POSTGRES_PASSWORD: ${REGISTRY_DB_PASSWORD}
    volumes:
      - registry-data:/var/lib/postgresql/data
    deploy:
      resources:
        limits: { memory: 256M, cpus: "0.5" }
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "agentregistry"]
      interval: 10s
      timeout: 3s
      retries: 5
```

## Caddy Routing

```
http://registry.localhost {
    reverse_proxy agentregistry:8080
}
```

Plus health check at `ai-workbench.localhost/api/health/agentregistry`.

## Artifact Registration

Each workbench component gets a declarative YAML file in `registry/`:

```
registry/
  mcp-servers.yaml      # code-agent, enrichment-agent, directory-mcp,
                         # ticketing-mcp, magic-fetch
  agents.yaml           # code-agent, enrichment-agent (agent records)
  infrastructure.yaml   # open-terminal, litellm, phoenix, chroma
```

Example:

```yaml
apiVersion: ar.dev/v1alpha1
kind: MCPServer
metadata:
  name: workbench/code-agent
  version: "1.0.0"
spec:
  title: Code Execution Agent
  description: >
    MCP tool server that executes Python code in an Open Terminal
    sandbox.  Produces downloadable files at files.localhost.
  packages:
    - registryType: oci
      identifier: ai-workbench-code-agent:latest
      runtimeHint: docker
      transport: { type: sse }
---
apiVersion: ar.dev/v1alpha1
kind: MCPServer
metadata:
  name: workbench/ticketing-mcp
  version: "1.0.0"
spec:
  title: the ticketing system API
  description: >
    Direct MCP bridge to the ticketing system REST API.  CRUD for incidents,
    users, CMDB CIs, and catalog items.
  packages:
    - registryType: oci
      identifier: ai-workbench-ticketing-mcp:latest
      runtimeHint: docker
      transport: { type: sse }
```

Applied with: `arctl apply -f registry/`

## Enterprise Capability Map

The registry becomes the authoritative source for mapping workbench
capabilities to enterprise equivalents:

```
┌──────────────────┬────────────────────┬─────────────────────────┐
│ Capability       │ Workbench (local)  │ Enterprise (target)     │
├──────────────────┼────────────────────┼─────────────────────────┤
│ Registry         │ Agentregistry      │ Agentregistry (scaled)  │
│ Orchestrator     │ Open WebUI         │ Copilot Studio / ADK    │
│ Models           │ LiteLLM → OpenAI   │ Azure AI Foundry        │
│ Code execution   │ Open Terminal      │ Container Apps / ACA    │
│ ticketing tools       │ ticketing-mcp     │ the ticketing system IntegHub     │
│ Directory        │ directory-mcp           │ Entra ID / Graph API    │
│ Data enrichment  │ enrichment-agent   │ Databricks / Fabric     │
│ Knowledge/RAG    │ Chroma             │ AI Search / Pinecone    │
│ Observability    │ Phoenix            │ Azure Monitor / Splunk  │
│ Policy engine    │ OPA (planned)      │ Immuta / Purview        │
│ Guardrails       │ NeMo (planned)     │ Content Safety / Lakera │
│ Identity/Access  │ Open WebUI auth    │ Entra ID + Managed ID   │
│ Reverse proxy    │ Caddy              │ API Management / Envoy  │
└──────────────────┴────────────────────┴─────────────────────────┘
```

Each row is a **capability slot**.  The registry tracks what fills each slot
today, and the enterprise column documents where the workbench pattern maps
when scaling.  This table lives in the registry as metadata and in this doc
as the strategic reference.

## Implementation Phases

### Phase 1 — Registry service (this sprint)

- [ ] Add `registry-db` and `agentregistry` to compose
- [ ] Update `bootstrap.py`: Caddy block, health route, status page dot
- [ ] Update `.env.example`: `REGISTRY_DB_PASSWORD`, `REGISTRY_JWT_KEY`,
      `AGENTREGISTRY_IMAGE`
- [ ] Add to `WORKBENCH_NO_PROXY`: `agentregistry`, `registry-db`
- [ ] Verify web UI at `registry.localhost`

### Phase 2 — Populate catalog

- [ ] Write declarative YAML for all existing components
- [ ] Install `arctl` on host (or run via Docker)
- [ ] `arctl apply -f registry/` to seed the catalog
- [ ] Verify artifacts visible in web UI

### Phase 3 — Enterprise mapping (future)

- [ ] Add capability-map metadata to registry artifacts
- [ ] Document enterprise equivalents per capability slot
- [ ] Evaluate semantic search (enable pgvector + embeddings via LiteLLM)
- [ ] Explore `arctl configure` for IDE integration

### Phase 4 — Governance layer (future)

- [ ] Add OPA for policy enforcement
- [ ] Add NeMo Guardrails for content safety
- [ ] Map governance controls to enterprise equivalents

## Resource Budget

| Service | Memory | CPU | Notes |
|---------|--------|-----|-------|
| `agentregistry` | 512 MB | 1.0 | Go binary + embedded UI |
| `registry-db` | 256 MB | 0.5 | pgvector-enabled Postgres |
| **Total new** | **768 MB** | **1.5** | |

Current workbench total is ~5.5 GB.  This adds ~14% memory overhead.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Agentregistry is early-stage | Breaking changes in API/schema | Pin image version, test before upgrading |
| Deploy UI buttons confuse users | Click → error | Don't mount Docker socket; document in status page |
| Registry metadata drifts from compose files | Catalog becomes stale | Generate registry YAML from compose manifests in CI |
| pgvector image doesn't build on ARM | Blocks startup | Verified: `pgvector/pgvector:pg16` has ARM64 images |
| Extra memory pressure | Laptop performance | Both services have resource limits; can stop with `--profile` |

## Open Questions

1. Should we generate registry YAML automatically from `compose.mcp.yaml`
   files (via `bootstrap.py`), or maintain them separately?
2. Is `arctl` useful enough for v1, or should the web UI be the primary
   interface?
3. When should semantic search be enabled (requires embeddings provider —
   could route through LiteLLM to keep traffic local)?

## Phase 2: Registry-Based Tool Discovery (Implemented)

The registry is now a **functional discovery layer**, not just a static catalog.

### Architecture

```
bootstrap.py  ──►  config/workbench-tools.json  (static baseline)
                          │
                          ├──►  code-agent       (dynamic system prompt)
                          ├──►  enrichment-agent  (advanced data access section)
                          └──►  open-terminal     (discovery module + env vars)
                                     │
                                     ▼
                              mcpo OpenAPI specs  (live tool details)
                              agentregistry API   (full catalog browsing)
```

### How it works

1. **Bootstrap generates `config/workbench-tools.json`** — reads `config/mcpo/mcpo.json`
   for proxied server names, enriches with descriptions from `registry/catalog.yaml`.
2. **code-agent** lazy-loads tool details from mcpo OpenAPI on first request, builds
   the "Workbench Data Sources" system prompt section dynamically. TTL-cached.
3. **enrichment-agent** references the discovery module for batch data operations.
4. **open-terminal** has `/opt/workbench/discovery.py` mounted with `PYTHONPATH` set.
   Generated code can `import discovery` to find and call tools.

### Adding a new MCP tool

1. Add to `config/mcpo/mcpo.json`
2. Add to `registry/catalog.yaml`, run `python3 registry/apply.py`
3. Run `make bootstrap` to regenerate `config/workbench-tools.json`
4. Restart services — agents auto-discover the new tool

No code changes needed in agents or terminal.
