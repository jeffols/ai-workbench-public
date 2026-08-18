# Using the workbench from a laptop terminal

Goal: interact with the workbench from a CLI on your laptop the way Claude
Code / GitHub Copilot CLI do — LLM-assisted coding with access to your MCP
tool rack (directory-agent, ticketing-agent, code-agent, enrichment-agent,
magic-fetch, and the raw data servers).

Two levers make this easy:

1. The workbench's **LiteLLM** (`http://localhost:4000/v1`, auth with
   `LITELLM_MASTER_KEY`) is already an OpenAI-compatible proxy in front of
   Azure OpenAI / OpenAI / Anthropic. Any CLI that speaks OpenAI-compat can
   use it.
2. The workbench's **MCP tools** are streamable-HTTP at
   `http://localhost:8090/mcp/<slug>/mcp` (via Caddy). Any MCP-capable CLI
   can connect.

> **Security caveat (read first).** Today every MCP endpoint has
> `auth: mode: none` and `LITELLM_MASTER_KEY` is a god credential (see
> `docs/architectural-review-2026-04-24.md`, P0-3 and P1-3). Everything
> below is safe on `localhost`. Before exposing the workbench over Tailscale
> / VPN / any non-localhost interface, do P0-3 (per-caller LiteLLM virtual
> keys) and P1-3 (bearer auth on MCP via Caddy).

## Prerequisites

- Workbench running (`make up`) and healthy (`make health`).
- Azure OpenAI configured in `.env` with `azure-strong` (or another alias)
  reachable via LiteLLM. Sanity check:

```bash
curl -s http://localhost:4000/v1/models \
  -H "Authorization: Bearer $(grep ^LITELLM_MASTER_KEY .env | cut -d= -f2)" \
  | jq '.data[].id'
```

You should see `azure-strong`, `azure-fast`, `embeddings`, etc.

- Shell env shortcut (source from `~/.zshrc` / `~/.bashrc` or export per
  session):

```bash
export WORKBENCH_LITELLM_URL="http://localhost:4000/v1"
export WORKBENCH_LITELLM_KEY="$(grep ^LITELLM_MASTER_KEY ~/src/ai-workbench/.env | cut -d= -f2)"
export WORKBENCH_MCP_BASE="http://localhost:8090/mcp"
```

## Can I use GitHub Copilot CLI with Azure OpenAI?

**Yes.** As of 2026-04-07, GitHub Copilot CLI supports BYOK with Azure
OpenAI, generic OpenAI-compatible providers (so the workbench's LiteLLM
works directly), OpenAI, Anthropic, AWS Bedrock, Google, xAI, and local
models like Ollama. When BYOK is configured the CLI does **not** require a
GitHub Copilot subscription or GitHub authentication.

### Option A — Copilot CLI against Azure OpenAI directly

```bash
export COPILOT_PROVIDER_TYPE=azure
export COPILOT_PROVIDER_BASE_URL="https://<your-resource>.openai.azure.com/openai/deployments/<your-deployment>"
export COPILOT_PROVIDER_API_KEY="<your-azure-key>"
export COPILOT_MODEL="<your-deployment-name>"

copilot
```

Skips the workbench entirely. Use when you want Copilot CLI with Azure and
don't care about MCP tools from the workbench.

### Option B — Copilot CLI against the workbench's LiteLLM (recommended)

```bash
export COPILOT_PROVIDER_TYPE=openai
export COPILOT_PROVIDER_BASE_URL="$WORKBENCH_LITELLM_URL"
export COPILOT_PROVIDER_API_KEY="$WORKBENCH_LITELLM_KEY"
export COPILOT_MODEL="azure-strong"   # or any LiteLLM alias

copilot
```

This routes through LiteLLM so you inherit the workbench's trace emission
(Phoenix sees the calls), cost controls you add later (P0-3), and
provider-swap flexibility.

MCP support in Copilot CLI is evolving — check `copilot --help` for the
current `mcp` subcommand. The non-Copilot options below have more mature
MCP stories today.

## OpenAI Codex CLI (closest to a "Claude Code for Azure" feel, full MCP)

Install:

```bash
npm install -g @openai/codex
# or: brew install --cask codex
```

Config at `~/.codex/config.toml`:

```toml
model = "azure-strong"
model_provider = "workbench"
model_reasoning_effort = "medium"

# Route through the workbench's LiteLLM (recommended — MCP tool traces land in Phoenix)
[model_providers.workbench]
name = "AI Workbench LiteLLM"
base_url = "http://localhost:4000/v1"
env_key = "WORKBENCH_LITELLM_KEY"
wire_api = "chat"   # LiteLLM supports chat-completions; use "responses" only if you hit Azure direct with v1 API

# ── MCP servers from the workbench ────────────────────────────────────────
[mcp_servers.directory-agent]
type = "streamable_http"
url = "http://localhost:8090/mcp/directory-agent/mcp"

[mcp_servers.ticketing-agent]
type = "streamable_http"
url = "http://localhost:8090/mcp/ticketing-agent/mcp"

[mcp_servers.code-agent]
type = "streamable_http"
url = "http://localhost:8090/mcp/code-agent/mcp"

[mcp_servers.enrichment-agent]
type = "streamable_http"
url = "http://localhost:8090/mcp/enrichment-agent/mcp"

[mcp_servers.directory-mcp]
type = "streamable_http"
url = "http://localhost:8090/mcp/directory-mcp/mcp"

[mcp_servers.ticketing-mcp]
type = "streamable_http"
url = "http://localhost:8090/mcp/ticketing-mcp/mcp"
```

Alternative: go direct to Azure (skip LiteLLM). Use this if you want
stricter Azure compliance boundaries:

```toml
[model_providers.azure]
name = "Azure OpenAI"
base_url = "https://<your-resource>.openai.azure.com/openai/v1"
env_key = "AZURE_OPENAI_API_KEY"
wire_api = "responses"
```

Test:

```bash
codex "use directory-agent to find users two levels below Jane Doe"
```

## opencode (terminal TUI, MCP-native, multi-provider)

Install: see `opencode.ai`.

Config at project root `opencode.json` (or `~/.config/opencode/opencode.json`):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "workbench": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "AI Workbench LiteLLM",
      "options": {
        "baseURL": "http://localhost:4000/v1",
        "apiKey": "{env:WORKBENCH_LITELLM_KEY}"
      },
      "models": {
        "azure-strong": {},
        "azure-fast": {},
        "anthropic-strong": {}
      }
    }
  },
  "mcp": {
    "directory-agent": {
      "type": "remote",
      "url": "http://localhost:8090/mcp/directory-agent/mcp",
      "enabled": true
    },
    "ticketing-agent": {
      "type": "remote",
      "url": "http://localhost:8090/mcp/ticketing-agent/mcp",
      "enabled": true
    },
    "code-agent": {
      "type": "remote",
      "url": "http://localhost:8090/mcp/code-agent/mcp",
      "enabled": true
    },
    "enrichment-agent": {
      "type": "remote",
      "url": "http://localhost:8090/mcp/enrichment-agent/mcp",
      "enabled": true
    }
  }
}
```

When bearer auth lands (P1-3), add per-server `headers`:

```jsonc
"headers": { "Authorization": "Bearer {env:WORKBENCH_MCP_TOKEN}" }
```

## Goose (CLI + desktop, MCP-first)

Install from `block.github.io/goose/`. Configure:

```bash
goose configure
# Provider: OpenAI-compatible
# Base URL: http://localhost:4000/v1
# API Key: <WORKBENCH_LITELLM_KEY>
# Model: azure-strong
```

Goose also has native Azure OpenAI provider support (`AZURE_OPENAI_ENDPOINT`
+ `AZURE_OPENAI_DEPLOYMENT_NAME` + `AZURE_OPENAI_API_KEY`) if you prefer
to skip LiteLLM.

Add the workbench's MCP tools as "extensions":

```bash
goose configure   # pick "Add Extension" → "Remote Extension (streamable-http)"
# Name: directory-agent
# URL: http://localhost:8090/mcp/directory-agent/mcp
# (repeat for each tool)
```

## aider (focused pair-programming)

Install: `pip install aider-chat`.

Use with workbench LiteLLM:

```bash
aider --model openai/azure-strong \
      --openai-api-base http://localhost:4000/v1 \
      --openai-api-key "$WORKBENCH_LITELLM_KEY"
```

Or direct to Azure:

```bash
aider --model azure/<deployment-name> \
      --api-key azure=<your-azure-key>
```

aider's MCP support is limited / experimental; prefer Codex CLI or opencode
if MCP access matters.

## Claude Code (note)

Anthropic-only for reasoning — cannot use Azure OpenAI. If you have an
Anthropic key in the workbench, Claude Code works against Anthropic
directly and can also connect to the workbench's MCP tools via
`claude mcp add --transport http <name> http://localhost:8090/mcp/<slug>/mcp`.
Useful as a second tool alongside an Azure-backed CLI.

## Which should I pick?

| Want… | Pick |
|---|---|
| Closest to "GitHub Copilot CLI" experience, Azure native | **Copilot CLI (BYOK)** |
| Closest to "Claude Code in a terminal" with full MCP | **OpenAI Codex CLI** |
| MCP-first multi-provider TUI, opinionated UX | **opencode** |
| Enterprise-flavor CLI + desktop + Entra auth path | **Goose** |
| Minimal, focused pair-programming | **aider** |

If I had to pick one for this workbench today: **OpenAI Codex CLI** — the
single `config.toml` covers Azure routing (direct or via LiteLLM) and all
MCP servers with the cleanest syntax, and it's actively developed with
first-party Azure guidance from Microsoft Learn.

## Smoke-test checklist

After wiring whichever CLI you chose:

1. `make ps` — workbench services up.
2. `curl -s http://localhost:4000/v1/models -H "Authorization: Bearer $WORKBENCH_LITELLM_KEY"` — model aliases listed.
3. `curl -s http://localhost:8090/mcp/directory-agent/mcp -X POST -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"laptop","version":"1"}}}'` — returns an MCP session id.
4. From the CLI, ask a workbench question (e.g. `codex "ask directory-agent who reports to Jane Doe"`).
5. Open `http://phoenix.localhost` — the call should appear as a trace.

## When you eventually expose the workbench beyond localhost

Prerequisites, in order:

1. **P0-2** — pin image tags so your laptop and the host box run the same versions.
2. **P0-3** — replace `LITELLM_MASTER_KEY` with a per-caller virtual key for your laptop CLI; set a budget.
3. **P1-3** — add bearer auth to Caddy's `/mcp/<slug>/*` routes; generate a per-caller MCP token; add `Authorization` headers to the CLI's MCP config.
4. Only then: Tailscale / VPN / ingress.

Reason: today's `auth: mode: none` + shared master key pattern becomes a
material risk the moment traffic leaves `127.0.0.1`.
