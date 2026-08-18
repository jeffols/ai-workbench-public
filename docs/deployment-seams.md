# Deployment Seams

> **Status: implemented pattern.**
> The workbench uses Compose profiles to let operators swap components
> at well-defined architectural boundaries — called **seams** — without
> rewiring the rest of the stack.

---

## What is a seam?

A **seam** is a boundary in the architecture where one component can be
replaced by another that exposes the same interface to its neighbours.
The rest of the stack doesn't need to change.

```
                    ┌─ Open WebUI + Chroma   (lean / fast / low memory)
  chat.localhost ──▶│
                    └─ Onyx + Vespa + Redis   (rich / deep RAG / enterprise search)
                           │
                           ▼
                       LiteLLM :4000/v1      ← shared model gateway (the seam surface)
                           │
                       Phoenix :6006         ← shared observability
```

Each side of a seam speaks the same protocol (here: OpenAI-compatible
`/v1` API). That's what makes the swap possible.

---

## Why seams matter

Different use cases optimize for different things:

| Axis | Lean profile | Rich profile |
|------|-------------|--------------|
| **Startup time** | Seconds | Minutes (model servers, indexing) |
| **Memory** | ~2.5 GB | 8–16 GB |
| **RAG depth** | Basic vector search (Chroma) | Hybrid search, knowledge graphs, 60+ connectors (Vespa) |
| **Enterprise search** | None | Full-text across Slack, Confluence, GitHub, … |
| **MCP tools** | Native (auto-discovered) | Requires OpenAPI action wrappers |
| **Configuration surface** | `.env` only | `.env` + Onyx admin UI |

A single deployment can't optimize for all of these simultaneously.
Seams let you choose the right trade-off for your situation — a laptop
demo vs. a team deployment vs. a production pilot — without maintaining
separate repos or diverging codebases.

---

## Current seam points

### 1. Chat Frontend (implemented)

| Profile | Services | Optimizes for |
|---------|----------|---------------|
| `openwebui` (default) | Open WebUI, Chroma | Speed, simplicity, low memory, native MCP tools |
| `onyx` | Onyx API, background worker, web server, PostgreSQL, Vespa, Redis, Nginx | Deep RAG, enterprise search, 60+ connectors |

**Seam surface:** Both talk to LiteLLM via `http://litellm:4000/v1`
(OpenAI-compatible API). Caddy routes `chat.localhost` to whichever
frontend is active.

**Controlled by:** `CHAT_FRONTEND` in `.env` (`openwebui` or `onyx`).

```bash
# Lean — daily driver on a laptop
CHAT_FRONTEND=openwebui
make up

# Rich — enterprise RAG pilot
CHAT_FRONTEND=onyx
make up-onyx
```

### 2. Model Gateway (future seam)

| Option | Optimizes for |
|--------|---------------|
| LiteLLM (current) | Simplicity, open-source, cost tracking |
| Portkey | Guardrails, enterprise security, semantic caching |

**Seam surface:** Both expose an OpenAI-compatible `/v1` API.
Open WebUI and Onyx both point at `http://<gateway>:4000/v1`.

### 3. Guardrails / DLP (implemented — opt-in)

Based on a reference governance-plane design with five swappable seams:
gateway, guardrails/DLP, policy, tool-gate, observability. ai-workbench adopts
the guardrails/DLP seam first since it's the highest-value, lowest-effort gap
(PII/PHI leakage into model prompts) and needs no model to work.

| Layer | Fill | Optimizes for |
|-------|------|---------------|
| PII/PHI DLP | Microsoft Presidio (analyzer + anonymizer) | OSS, offline, no model needed |
| Prompt-injection / jailbreak | Regex heuristic (`config/litellm/guardrails/injection_guardrail.py`) | Zero-cost, always-on, model-free |
| *(future)* LLM-based injection check | e.g. an LLM self-check rail | Higher recall, needs a model |
| *(future)* Policy PDP (execution control) | Open Policy Agent — step budgets, risk thresholds | Runaway-agent protection |
| *(future)* Tool-gate allowlist | shim in front of `mcpo`, or a dedicated MCP gateway | No direct DB/shell from agents |

**Seam surface:** Both guardrails run as native LiteLLM `guardrails:` entries
(`pre_call` mode) — enforced once at the gateway, so every frontend (Open WebUI,
Onyx) is covered without any frontend-side changes.

**Controlled by:** `GUARDRAILS_ENABLED` in `.env` (bootstrap only emits the
`guardrails:` block in `config/litellm/config.yaml` when `true`) and the
`guardrails` Compose profile.

```bash
# Lean — guardrails off (default)
GUARDRAILS_ENABLED=false
make up

# DLP + injection heuristic on
GUARDRAILS_ENABLED=true
make up-guardrails
```

See [guardrails-findings-2026-07-22.md](guardrails-findings-2026-07-22.md) for
the full investigation, the seam-by-seam design comparison, and the rationale
for starting with DLP + injection heuristic only.

### 4. Observability (stable — no swap needed today)

Phoenix covers tracing for all current and planned components via OTEL.
Both LiteLLM and Portkey can emit OTEL traces. No seam needed yet.

---

## How profiles implement seams

Compose profiles are the mechanism. Each seam point maps to a set of
mutually exclusive profiles:

```yaml
# Lean frontend
openwebui:
  profiles: [openwebui]      # only starts with COMPOSE_PROFILES=openwebui
chroma:
  profiles: [openwebui]

# Rich frontend
onyx-api-server:
  profiles: [onyx]           # only starts with COMPOSE_PROFILES=onyx
onyx-web-server:
  profiles: [onyx]
# ... etc
```

The Makefile targets set the right profile combination:

```makefile
up:        COMPOSE_PROFILES=openwebui,tools   # lean daily driver
up-onyx:   COMPOSE_PROFILES=onyx,tools        # rich enterprise
```

Bootstrap reads `CHAT_FRONTEND` from `.env` and generates:
- **Caddyfile** — `chat.localhost` routes to the active frontend
- **Tool connections** — only generated for Open WebUI (Onyx manages
  its own tool config via admin UI)

---

## Design principles for seams

1. **Shared infrastructure stays shared.** LiteLLM, Phoenix, and Caddy
   are not behind a seam — they're always on. This keeps observability
   and model routing consistent regardless of which frontend is active.

2. **The seam surface is a standard protocol.** OpenAI `/v1` API for
   model gateways. OTEL for traces. HTTP for reverse-proxy routing.
   No custom glue.

3. **One `.env` variable controls the swap.** `CHAT_FRONTEND=openwebui`
   or `CHAT_FRONTEND=onyx`. Bootstrap handles the rest.

4. **Each profile is self-contained.** The Onyx profile brings its own
   PostgreSQL, Redis, Vespa — it doesn't share databases with the lean
   profile. This avoids state entanglement.

5. **Seams are opt-in.** If you never set `CHAT_FRONTEND=onyx`, the
   workbench behaves exactly as before. No new complexity for the
   default case.

---

## Adding a new seam

When you identify a new swappable boundary:

1. Verify both sides speak the same protocol at the seam surface
2. Create a new Compose profile for the alternative
3. Add a `.env` variable to select between options
4. Update `bootstrap.py` to generate the right config for each option
5. Add a Makefile target for convenience
6. Document the seam in this file

---

## Common questions

**Can I run both frontends at once?**
No. `chat.localhost` can only point at one backend. Running both would
also double memory usage. Use `make down` before switching.

**Do my MCP tools work with Onyx?**
Not natively. Open WebUI discovers MCP tools via the generated
`TOOL_SERVER_CONNECTIONS`. Onyx uses its own agent/action framework.
You'd need to register your tools as Onyx OpenAPI actions or wait for
native MCP support in Onyx.

**What about data migration?**
Chat history lives in each frontend's own volume. Switching frontends
starts fresh. RAG documents indexed in Chroma are not visible to Onyx's
Vespa, and vice versa. This is by design — the profiles are independent
deployment postures, not shared-state configurations.
