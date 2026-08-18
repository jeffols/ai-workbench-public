# Local AI Workbench Bootstrap

This repo bootstraps a **local AI workbench** on an ARM Mac running Rancher Desktop.

It gives you:

- **Open WebUI** as the chat frontend
- **Open Notebook** as a NotebookLM-style research interface
- **LiteLLM Proxy** as the single model gateway for commercial providers
- **Caddy** as the reverse proxy / MCP tool front door
- **SurrealDB** as the database backend for Open Notebook
- **Chroma** for retrieval storage
- **Phoenix** for local tracing/observability
- **MCP Tool Rack** — plug-in MCP servers, each packaged as an OCI container
- **mcpo** — MCP-to-OpenAPI proxy; lets sandbox code call MCP tools via REST
- **Python helper scripts** for bootstrap, validation, and quick health checks

## Architecture

```
  User → Open WebUI :3000 → Caddy :8090 → MCP Tools (slug-based routing)
              │                  │
              ├── LiteLLM :4000 ─┤──→ OpenAI / Anthropic / Azure APIs
              ├── Chroma         │
              └── Phoenix :6006 ◄┘── OTEL traces from all services

  Agents (code-agent) → LiteLLM (reasoning)
       │                       │
       ├── MCP servers (mcp-fetch, mcp-time)      └── External APIs
       └── Open Terminal (sandboxed Python execution)
                │
                └── mcpo (MCP → REST proxy) → MCP servers

  Open Notebook :8502 (optional) → LiteLLM → APIs
       └── SurrealDB
```

> 📐 Full Mermaid diagram with all 16 services: **[docs/architecture.md](docs/architecture.md)**

- **Open WebUI** talks to MCP tools through **Caddy** (slug-based routing: `/mcp/<slug>/*`).
- **Agents** use ReAct loops via **LiteLLM** for reasoning, calling **data servers** and **Open Terminal** as tools.
- **All instrumented services** emit OTEL traces to **Phoenix**.
- Only **Open WebUI** (`:3000`), **Caddy** (`:8090` + `:80`), and optionally **Open Notebook** (`:8502`) are host-exposed.

### Subdomain access

`.localhost` domains resolve to `127.0.0.1` automatically (RFC 6761) — no `/etc/hosts` needed.

| URL | Service |
|-----|---------|
| http://ai-workbench.localhost | Status dashboard |
| http://chat.localhost | Open WebUI |
| http://litellm.localhost | LiteLLM API |
| http://phoenix.localhost | Phoenix traces |
| http://notebook.localhost | Open Notebook |

> 📖 Design principles and conceptual framework: **[docs/design-principles.md](docs/design-principles.md)**
>
> 📐 Swappable components and deployment postures: **[docs/deployment-seams.md](docs/deployment-seams.md)**

## MCP Platform Contract

Every MCP tool in the rack is an **OCI container** with an **HTTP MCP endpoint**.
No stdio. No host.docker.internal hacks. If it's not a container, it doesn't belong.

### Tool Package Structure

```
services/<slug>/
├── Dockerfile          # OCI image
├── mcp-tool.yaml       # metadata manifest
├── compose.mcp.yaml    # Compose fragment (included by root)
└── server.py           # (or whatever your app is)
```

### mcp-tool.yaml (metadata manifest)

```yaml
slug: my-tool              # unique URL slug
name: My Tool              # human-readable name
owner: my-team             # team/person
description: What it does
transport: http
port: 8080                 # internal container port
mcp_path: /mcp             # MCP endpoint
health_path: /healthz      # health check
auth:
  mode: none               # none | bearer | api-key
risk_tier: internal
tags: [search, data]
depends_on_platform:       # optional platform deps
  - litellm
```

### Adding a new MCP tool

1. Create `services/<slug>/` with Dockerfile, server, mcp-tool.yaml, compose.mcp.yaml
2. Add `- path: ./services/<slug>/compose.mcp.yaml` to the `include:` section of `docker-compose.yml`
3. Run `python3 scripts/bootstrap.py --force`
4. Run `make up`
5. Check `make health` and `make catalog`

Bootstrap auto-generates:
- **Caddyfile** with a route for your slug
- **TOOL_SERVER_CONNECTIONS** so Open WebUI discovers the tool
- **catalog.json + catalog.html** for discoverability

## Compose Profiles

| Layer | Services | Starts with `make up`? |
|-------|----------|----------------------|
| Core platform | Caddy, LiteLLM, Phoenix | Always |
| Open WebUI frontend | Open WebUI, Chroma | Yes (`openwebui` profile — default) |
| Onyx frontend | Onyx API, background, web, PostgreSQL, Vespa, Redis, Minio, Nginx | No (`onyx` profile) |
| Notebook | Open Notebook, SurrealDB | No (`notebook` profile) |
| MCP Tools | code-agent, magic-fetch, mcp-fetch, mcp-time, open-terminal, mcpo, … | Yes (`tools` profile) |
| Guardrails | Presidio analyzer/anonymizer (DLP) + injection heuristic, enforced at LiteLLM | No (`guardrails` profile) |
| Debug | (reserved for inspectors) | No (`debug` profile) |

```bash
make up            # Open WebUI + tools (daily driver — lean, fast)
make up-onyx        # Onyx + tools (rich RAG, enterprise search)
make up-notebook    # Open WebUI + tools + Open Notebook
make up-guardrails  # Open WebUI + tools + Presidio DLP + injection heuristic
make up-core        # core only (no frontend, no MCP tools)
make up-full        # everything including debug
make down           # stop all
```

> 📐 **Deployment seams:** The Open WebUI / Onyx swap is the first example of the
> workbench's **seam architecture** — swappable components at well-defined boundaries.
> See **[docs/deployment-seams.md](docs/deployment-seams.md)** for the design rationale
> and how to add new seams.

## Why there is no Ollama in this version

This version is intentionally **commercial-models only**. That makes the stack lighter on a 16 GB Mac and avoids mixing host-native inference concerns into the first bootstrap.

You can add Ollama back later behind LiteLLM without changing the overall architecture.

## Repo layout

```text
.
├── .env.example
├── .gitignore
├── Makefile
├── README.md
├── compose.override.example.yml
├── docker-compose.yml
├── config/
│   ├── caddy/
│   │   └── Caddyfile          # generated by bootstrap.py
│   ├── catalog/
│   │   ├── catalog.json       # generated by bootstrap.py
│   │   └── index.html         # generated by bootstrap.py
│   ├── litellm/
│   │   └── config.yaml        # generated by bootstrap.py
│   └── mcpo/
│       └── mcpo.json          # mcpo server config (fetch + time)
├── scripts/
│   ├── bootstrap.py
│   ├── healthcheck.py
│   └── validate_env.py
└── services/
    ├── code-agent/
    │   ├── Dockerfile
    │   ├── compose.mcp.yaml
    │   ├── mcp-tool.yaml
    │   ├── requirements.txt
    │   └── server.py
    └── mcp-fetch/
        ├── Dockerfile
        ├── compose.mcp.yaml
        ├── mcp-tool.yaml
        ├── requirements.txt
        └── server.py
```

## Prereqs

1. Rancher Desktop using **dockerd (moby)**.
2. Kubernetes **disabled**.
3. A valid **OpenAI API key** or **Azure OpenAI** credentials (for embeddings).
4. Optionally, an **Anthropic API key** for Anthropic chat models.

### Embedding provider

RAG embeddings require either an OpenAI API key (uses `text-embedding-3-small` by
default) or Azure OpenAI with `AZURE_EMBEDDING_MODEL` configured.  Anthropic alone
is not enough because Anthropic does not offer embedding models.

## Quick start

```bash
python3 scripts/bootstrap.py
python3 scripts/validate_env.py
make up
make health
```

Then open:

- Open WebUI: http://localhost:3000
- Open Notebook: http://localhost:8502
- MCP Catalog: http://localhost:8090/catalog/

## First-run setup

### 1. Create `.env`

`bootstrap.py` will create `.env` from `.env.example` if it does not already exist and will generate secrets for:

- `LITELLM_MASTER_KEY`
- `WEBUI_SECRET_KEY`
- `WEBUI_ADMIN_PASSWORD`
- `OPEN_NOTEBOOK_ENCRYPTION_KEY`

### 2. Add real provider keys

Edit `.env` and set at least:

```env
OPENAI_API_KEY=...
```

Optional:

```env
ANTHROPIC_API_KEY=...
```

### 3. Regenerate config after env changes

```bash
python3 scripts/bootstrap.py --force
make up
```

This re-renders all generated config: LiteLLM config, Caddyfile, catalog, and TOOL_SERVER_CONNECTIONS.

## Default model aliases

The generated LiteLLM config creates aliases like these when keys are present:

- `openai-fast`
- `openai-strong`
- `anthropic-fast`
- `anthropic-strong`
- `embeddings`

Set `DEFAULT_CHAT_MODEL` in `.env` to one of those aliases.

## Services

### Caddy (MCP Tool Front Door)

- Host-exposed on `127.0.0.1:${CADDY_PORT}` (default 8090)
- All MCP tools reachable at `/mcp/<slug>/*`
- Static catalog at `/catalog`
- Health at `/health`

### Open WebUI

- Host-exposed on `127.0.0.1:${OPENWEBUI_PORT}`
- Signup disabled
- Admin user created from `.env` on first startup
- MCP tools discovered via `TOOL_SERVER_CONNECTIONS` (generated from manifests)

### Open Notebook

- Host-exposed on `127.0.0.1:${OPEN_NOTEBOOK_PORT}`
- NotebookLM-style research interface
- Uses SurrealDB for persistence

#### Connecting Open Notebook to LiteLLM

After first boot, open Open Notebook at http://localhost:8502 and configure the AI provider:

1. Go to **Settings → AI Provider**
2. Click **Add Credential** and select **OpenAI-Compatible**
3. Set **Base URL** to `http://litellm:4000/v1`
4. Set **API Key** to your `LITELLM_MASTER_KEY` value (from `.env`)
5. Save and register the models you want to use (e.g. `openai-fast`, `openai-strong`)

#### Save/load model configuration via API

Use `scripts/open_notebook_model_config.py` to export/import model config and set defaults.

```bash
# Export models + defaults
python3 scripts/open_notebook_model_config.py export --output /tmp/open-notebook-models.json

# Import and apply defaults from file (or fallback defaults)
python3 scripts/open_notebook_model_config.py import --input /tmp/open-notebook-models.json

# Set defaults explicitly (defaults to azure-strong / azure-strong / embeddings)
python3 scripts/open_notebook_model_config.py set-defaults
```

### LiteLLM

- Internal only by default
- Routes OpenAI/Anthropic/Azure requests
- Emits tracing to Phoenix

### Guardrails (opt-in — `guardrails` profile)

- **Presidio DLP** (`presidio-analyzer` + `presidio-anonymizer`) redacts PII/PHI
  before it reaches the model, enforced as a LiteLLM `pre_call` guardrail —
  covers every frontend (Open WebUI, Onyx) automatically.
- **Prompt-injection heuristic** — an always-on, model-free regex guardrail
  (`config/litellm/guardrails/injection_guardrail.py`) blocks obvious
  injection/jailbreak attempts before the model call.
- Enable with `GUARDRAILS_ENABLED=true` in `.env`, then `make up-guardrails`.
  Both checks run entirely offline — no model required.
- See **[docs/deployment-seams.md](docs/deployment-seams.md)** for the full
  seam writeup and **[docs/guardrails-findings-2026-07-22.md](docs/guardrails-findings-2026-07-22.md)**
  for the design rationale.

### Chroma / SurrealDB / Phoenix

- Internal only by default
- Chroma: retrieval for Open WebUI
- SurrealDB: persistence for Open Notebook
- Phoenix: OTEL traces from Open WebUI and LiteLLM

## Optional: expose Phoenix locally

```bash
cp compose.override.example.yml docker-compose.override.yml
make up
```

Publishes:

- Phoenix UI: `127.0.0.1:6006`

## Notes that will save you time

### 1. Open WebUI persists settings

Some Open WebUI settings persist in its data volume. If an env change does not seem to apply after first boot, update it in the UI or recreate the Open WebUI volume.

### 2. Backend services mostly stay on the internal network

LiteLLM is published on `127.0.0.1:4000` for local clients. Chroma, Phoenix, and all MCP tools stay off the host by default unless you opt into an override.

### 3. All generated config comes from bootstrap.py

Do not hand-edit `config/litellm/config.yaml`, `config/caddy/Caddyfile`, or `config/catalog/`. Change `.env` or `mcp-tool.yaml` then run `bootstrap.py --force`.

### 4. TOOL_SERVER_CONNECTIONS is generated

Open WebUI's MCP tool connections are auto-generated from `mcp-tool.yaml` manifests. No more hardcoded JSON blobs in docker-compose.yml.

### 5. Inter-service connectivity failing? Check `WORKBENCH_NO_PROXY`

Every Compose service hostname must appear in `WORKBENCH_NO_PROXY` in `.env`, otherwise the corporate proxy intercepts the traffic. This variable is intentionally named differently from `NO_PROXY` to avoid collisions with host environment variables.

### 6. Memory pressure on constrained machines

All services have memory limits (`deploy.resources.limits.memory`).  The default
footprint is approximately:

| Layer | Memory |
|-------|--------|
| Core (Caddy, LiteLLM, LiteLLM-DB, Phoenix, Chroma, Open WebUI) | ~2.9 GB |
| MCP Tools (7 tools + Open Terminal) | ~5.8 GB |
| Open Notebook + SurrealDB | ~768 MB |
| **Total (everything)** | **~6.1 GB** |

To reduce memory usage:

- **Skip Open Notebook**: use `make up` instead of `make up-notebook` (saves ~768 MB).
- **Skip MCP tools**: use `make up-core` (saves ~2.4 GB).
- **Tune Rancher Desktop VM**: allocate at least 8 GB to the VM.

### 7. Rotating generated secrets

`bootstrap.py --force` preserves existing secrets in `.env`.  To rotate a specific
secret, delete its value in `.env` (leave the key with an empty value) then run
`python3 scripts/bootstrap.py --force`.

### 8. Guardrails config is also generated

The `guardrails:` block in `config/litellm/config.yaml` is only emitted when
`GUARDRAILS_ENABLED=true`. Don't hand-edit it — change `.env` (`GUARDRAILS_ENABLED`,
`PRESIDIO_PII_ENTITIES`, `INJECTION_GUARDRAIL_ENABLED`) and rerun `bootstrap.py`.

## Useful commands

```bash
make bootstrap        # Generate .env + all config
make validate         # Pre-flight .env validation
make config           # Print merged compose config
make up               # Core + tools (daily driver — no Open Notebook)
make up-notebook      # Core + tools + Open Notebook
make up-core          # Core only (no tools, no notebook)
make up-full          # Everything including debug profile
make logs             # Tail logs (last 200 lines)
make ps               # List running containers
make health           # Run healthcheck against all services
make stats            # Tool-usage stats from Phoenix traces (--days 30, --json)
make down             # Stop everything
make clean-generated  # Delete generated config
make open             # Open status dashboard in browser (macOS)
make catalog          # Open MCP catalog in browser (macOS)
```

## License

This project is licensed under the [Apache License 2.0](LICENSE).

The Docker images it orchestrates (Open WebUI, LiteLLM, Chroma, Phoenix, Caddy, and others) are distributed under their own respective licenses.
