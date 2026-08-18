# Design Principles — Environment Adapter vs Agent Brain

> **Status: design principle / target direction.**
> This describes the mental model we use when designing and evolving the
> workbench. Not everything here is fully implemented today — see
> "Current state" below for what's real.

---

## The key concept

A useful separation for any agent-based system:

### Environment Adapter

A small layer that answers factual questions:

- "Where am I?"
- "What repo is this?"
- "What environment is configured?"
- "What identity is active?"
- "What services exist here?"

This can be a set of MCP tools, a local script the agent can call,
or a config file the agent reads. The point is that it's **queryable,
not guessed**.

### Agent Brain

Uses the adapter to:

- Plan steps
- Choose tools
- Interpret results
- Apply conventions

This keeps the agent from guessing and makes behavior predictable.

---

## Design goals

- **Clarity** — environment rules should be explicit, not buried in prompts
- **Reviewability** — configuration and conventions live in version-controlled files
- **Safety** — risky actions are constrained by design, not agent judgment
- **Composability** — tools can be mixed, matched, and extended independently

---

## Rule of thumb

When deciding where new logic belongs:

| Question | Belongs in |
|----------|-----------|
| "In *our repo*…" | Agent system prompt or config |
| "On *this machine*…" | Environment adapter (`.env`, `bootstrap.py`, runtime config) |
| "Always / never allow…" | Policy (sandbox restrictions, guardrails) |
| "Given X, return Y" | Tool (MCP server) |

---

## Current state (honest assessment)

What we have today, and where we're heading:

### Implemented ✅

- **MCP tools as capabilities** — data servers (`directory-mcp`, `ticketing-mcp`)
  expose narrow HTTP APIs; agents orchestrate them via ReAct loops
- **Specialist agents own domain logic** — `code-agent`, `directory-agent`,
  `ticketing-agent`, `enrichment-agent` each embed workflow knowledge
  in their system prompts
- **Environment config is centralized** — `.env` → `bootstrap.py` →
  generated config (LiteLLM, Caddy, tool connections, catalog)
- **Observability from day one** — all agents and services emit OTEL
  traces to Phoenix
- **Metadata manifests** — each tool publishes `mcp-tool.yaml` with
  slug, owner, transport, health path, risk tier
- **Sandbox isolation** — Open Terminal provides a sandboxed Python runtime
  with process-level isolation

### Partially implemented ⚠️

- **Environment adapter** — `.env` + `bootstrap.py` serve as the adapter
  today, but agents don't dynamically query "what's available"; they
  assume a fixed set of tools at build time
- **Tool reusability** — some tools are generic (`open-terminal`,
  `magic-fetch`), but enterprise adapters (`directory-mcp`, `ticketing-mcp`)
  are inherently environment-specific — and that's fine

### Not yet implemented 🔲

- **Explicit policy layer** — no command allowlist, protected-paths
  enforcement, or approval flows beyond sandbox module blocking
- **Dynamic environment context** — no runtime config file that agents
  load to discover their environment
- **Agent-level safety guardrails** — agents rely on prompt instructions,
  not enforced policy

---

## Anti-patterns we avoid

- Embedding secrets in agent prompts or config
- Letting agents infer environment state without querying it
- Hand-maintaining generated config (always use `bootstrap.py`)
- Coupling tool inventory to a single client (Open WebUI discovers
  tools via generated config, not hard-coded lists)

---

## Direction

When adding new capabilities, prefer:

1. **Declarative metadata** over hidden prompt assumptions
2. **Generated config** over hand-maintained routing
3. **Explicit runtime facts** over agent inference
4. **Separated safety checks** from agent reasoning (where feasible)
