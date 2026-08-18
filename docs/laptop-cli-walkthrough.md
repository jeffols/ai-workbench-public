# Walkthrough: configure your first laptop CLI against the workbench

Step-by-step to get OpenAI Codex CLI talking to your workbench's Azure
models (via LiteLLM) and MCP tool rack. ~20 minutes end-to-end.

**What you'll end up with:**

```
$ codex "ask ldap-agent who reports to Jeff Olsen"
# → runs on your laptop, reasons via Azure (through LiteLLM),
#   calls ldap-agent → ldap-mcp → AD, renders answer.
#   Trace visible at http://phoenix.localhost.
```

---

## 0. Prerequisites

- Workbench running: `make up && make health` (all green).
- You have a working `.env` with Azure OpenAI configured and at least one
  model alias visible via `curl http://localhost:4000/v1/models -H "Authorization: Bearer $(grep ^LITELLM_MASTER_KEY .env | cut -d= -f2)"`.
- Node.js 20+ **or** Homebrew on your laptop.
- You are running this CLI on the **same machine** that runs the workbench.
  (For remote access later, first do P0-3 and P1-3 from the review — see
  `docs/architectural-review-2026-04-24.md`.)

---

## 1. Export two env vars you'll reuse

Add to `~/.zshrc` (or run in your current shell):

```bash
export WORKBENCH_DIR="$HOME/src/ai-workbench"
export WORKBENCH_LITELLM_KEY="$(grep ^LITELLM_MASTER_KEY $WORKBENCH_DIR/.env | cut -d= -f2)"
```

Sanity check:

```bash
curl -sS http://localhost:4000/v1/models \
  -H "Authorization: Bearer $WORKBENCH_LITELLM_KEY" | jq -r '.data[].id'
```

You should see `azure-strong`, `azure-fast`, `embeddings`, etc. If you get
`Invalid API key`, re-check the master key value.

---

## 2. Install Codex CLI

Pick one:

```bash
# npm
npm install -g @openai/codex

# or: Homebrew
brew install --cask codex
```

Verify:

```bash
codex --version
```

---

## 3. Create `~/.codex/config.toml`

```bash
mkdir -p ~/.codex
```

Paste this into `~/.codex/config.toml`:

```toml
# Default model and provider
model = "azure-strong"
model_provider = "workbench"
model_reasoning_effort = "medium"

# Route through the workbench's LiteLLM — MCP tool traces land in Phoenix,
# and the same key/budget controls apply across WebUI + CLI + agents.
[model_providers.workbench]
name = "AI Workbench LiteLLM"
base_url = "http://localhost:4000/v1"
env_key = "WORKBENCH_LITELLM_KEY"
wire_api = "chat"

# ── Workbench MCP servers ─────────────────────────────────────────────────
# All seven tools exposed by the workbench, routed through Caddy.
# When P1-3 (MCP bearer auth) lands, add:
#   bearer_token_env_var = "WORKBENCH_MCP_TOKEN"
# under each server.

[mcp_servers.ldap-agent]
type = "streamable_http"
url = "http://localhost:8090/mcp/ldap-agent/mcp"

[mcp_servers.servicenow-agent]
type = "streamable_http"
url = "http://localhost:8090/mcp/servicenow-agent/mcp"

[mcp_servers.code-agent]
type = "streamable_http"
url = "http://localhost:8090/mcp/code-agent/mcp"

[mcp_servers.enrichment-agent]
type = "streamable_http"
url = "http://localhost:8090/mcp/enrichment-agent/mcp"

[mcp_servers.ldap-mcp]
type = "streamable_http"
url = "http://localhost:8090/mcp/ldap-mcp/mcp"

[mcp_servers.servicenow-mcp]
type = "streamable_http"
url = "http://localhost:8090/mcp/servicenow-mcp/mcp"

[mcp_servers.magic-fetch]
type = "streamable_http"
url = "http://localhost:8090/mcp/magic-fetch/mcp"
```

**Notes:**

- `env_key` must name an env var, not hold a literal key. Codex reads the
  env at launch; if `WORKBENCH_LITELLM_KEY` isn't exported in the shell
  that runs `codex`, you'll get a 401.
- `wire_api = "chat"` uses the OpenAI `/v1/chat/completions` shape, which
  LiteLLM exposes. (`responses` would be for direct Azure v1 API.)
- The seven MCP servers above match the seven `services/*/mcp-tool.yaml`
  manifests. If you add an eighth tool, add a matching `[mcp_servers.<slug>]`
  block here.

---

## 4. First run — smoke test

In a fresh terminal (so env vars are fresh):

```bash
source ~/.zshrc              # or re-export WORKBENCH_LITELLM_KEY
cd "$WORKBENCH_DIR"          # optional — codex picks up AGENTS.md here
codex
```

At the Codex prompt, try:

```
list the MCP tools you have access to
```

Expected: Codex should enumerate the seven workbench tools it loaded from
your config (`ldap-agent`, `servicenow-agent`, `code-agent`,
`enrichment-agent`, `ldap-mcp`, `servicenow-mcp`, `magic-fetch`).

If it doesn't see any: Codex logs MCP startup errors to stderr. Exit
(`Ctrl-D`), then run `codex --verbose` to see the MCP connection attempts.

---

## 5. Real query — end-to-end trace

Still in the Codex TUI:

```
use ldap-agent to find everyone in the HCSC_Data_Science group
```

You should see Codex decide to call `ldap-agent.org_query(...)`, which
spins the workbench's ReAct loop (ldap-agent → ldap-mcp → AD), then
render the answer.

Open `http://phoenix.localhost` in a browser. Under the `ldap-agent`
project, you should see a new trace: AGENT span → CHAIN (react_step) →
TOOL (find_group / group_members) → LLM (chat_completion). That's your
laptop CLI → workbench → Azure loop, fully observable.

---

## 6. Real workflow — spreadsheet enrichment

The motivating use case. In Codex:

```
I have a CSV of user logins:
login
jdoe
U123456
U987654

For each one, use enrichment-agent to look up the person's manager from
LDAP and their open ServiceNow incidents. Export the result as xlsx.
```

What should happen: Codex calls `enrichment-agent.enrich_data(task=...)`
with the CSV inline. The enrichment agent delegates to `ldap-agent` and
`servicenow-agent`, generates Python that iterates the rows via mcpo,
writes an xlsx, and returns a download URL. Codex surfaces the URL to you.

This is the deterministic-meets-LLM composition Open Terminal was built
for, driven from your laptop.

---

## 7. Optional — add an `AGENTS.md` for project-specific guidance

Codex merges `AGENTS.md` files from `~/.codex/`, the repo root, and the
current working directory. A useful starter at
`$WORKBENCH_DIR/AGENTS.md`:

```markdown
# AI Workbench — Codex guidance

This is the AI Workbench bootstrap repo.  Key conventions:

- **All config is generated from `.env` + `services/*/mcp-tool.yaml` by
  `scripts/bootstrap.py`.** Never hand-edit `config/litellm/config.yaml`,
  `config/caddy/Caddyfile`, or `config/catalog/*`. Edit `.env` or a
  manifest, then run `make bootstrap`.
- Images are pinned in `.env.example`. To freeze your currently-running
  digests, run `make pin-images`.
- Enterprise adapters (`ldap-mcp`, `servicenow-mcp`) are **read-only until
  a policy layer exists.** Do not add write tools without the policy layer.
- Agents migrate to LangGraph (see `docs/architectural-review-2026-04-24.md`
  P1-1).  New agents should be LangGraph, not hand-rolled ReAct.
- Tracing: every agent emits OpenInference spans to Phoenix. Preserve the
  existing span attributes when refactoring.

When writing code in `services/`, prefer the patterns already present in
sibling services.
```

---

## Troubleshooting

**401 from LiteLLM** — `WORKBENCH_LITELLM_KEY` isn't exported in the shell
running `codex`. Run `echo $WORKBENCH_LITELLM_KEY` in that terminal; if
empty, re-source your shell config.

**MCP server "connection refused"** — Caddy isn't up (`make ps | grep caddy`),
or you're hitting the wrong port. Workbench Caddy binds to `127.0.0.1:8090`.
From inside a container on the backend network the URL is
`http://caddy:8090/...`; from your laptop (host), it's
`http://localhost:8090/...`. Codex on the laptop uses the localhost form.

**MCP server "session expired" after idle** — the streamable-http session
is a workbench-side agent issue (module globals in
`services/*-agent/server.py`, review P0-1). Restart the agent:
`docker compose restart ldap-agent`.

**Model not found** — Codex model name must exist in LiteLLM. Run the
`curl /v1/models` check from §1. If you expect `azure-strong` and don't
see it, re-run `make bootstrap` with `AZURE_STRONG_MODEL` set.

**Calls don't appear in Phoenix** — LiteLLM is configured with Arize
callback (`config/litellm/config.yaml`, `callbacks: ["arize_phoenix"]`).
Only calls *through* LiteLLM trace. Direct calls to Azure bypass it.

---

## If you'd rather use GitHub Copilot CLI

Same path, different env-var flavor. Install `copilot` (`npm i -g
@github/copilot`), then:

```bash
export COPILOT_PROVIDER_TYPE=openai
export COPILOT_PROVIDER_BASE_URL="http://localhost:4000/v1"
export COPILOT_PROVIDER_API_KEY="$WORKBENCH_LITELLM_KEY"
export COPILOT_MODEL="azure-strong"
copilot
```

BYOK mode skips GitHub auth entirely. MCP server config lives in Copilot
CLI's own config — consult `copilot --help mcp` for the current syntax
(it's evolving).

For the full comparison of laptop CLI options (opencode, Goose, aider,
Claude Code) see `docs/laptop-cli-setup.md`.

---

## Next

Once you have one query running end-to-end, the natural follow-ups:

1. **Decide on the k8s scaffold.** Start a `charts/workbench/` directory
   against `kind` as a forcing function. Expect it to break until P0-1,
   P0-3, P1-1, P1-3 land.
2. **Pick P1-1 (LangGraph migration) for the `ldap-agent` pilot.**
   Smallest agent, cleanest surface. Migrating one agent tells you
   whether the shared-core pattern fits before you touch the other three.
3. **Start P1-2 (Agentregistry as source of truth).** Deadline 2026-06-24
   per committed decisions — if it hasn't been wired by then, delete it.
