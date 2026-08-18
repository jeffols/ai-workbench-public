## Direct mapping: paper → your LiteLLM + MCP + gateway stack

| Paper concept           | Your stack equivalent                                   | What it means                                                     |
| ----------------------- | ------------------------------------------------------- | ----------------------------------------------------------------- |
| **Agent loop**          | Open WebUI / Claude Code / custom agent runner          | Keep this thin. Do not over-engineer the “brain.”                 |
| **Model call**          | **LiteLLM Proxy**                                       | Central model router, budget control, provider abstraction.       |
| **Tool surface**        | **MCP servers**                                         | Your tools should register here, not be hardcoded into each app.  |
| **Permission system**   | **Gateway policy layer**                                | Missing/immature. This is the biggest platform gap.               |
| **Hooks**               | Gateway middleware / pre-tool / post-tool interceptors  | Needed for logging, policy, rewriting, blocking, approvals.       |
| **Skills**              | Repo-local prompt packs / `.claude/skills`-like folders | Low-cost reusable domain instructions.                            |
| **Plugins**             | Docker-compose profiles + manifests                     | Package MCP servers, skills, hooks, configs together.             |
| **Context compaction**  | Agent/runtime responsibility                            | LiteLLM does not solve this. Open WebUI may help, but not enough. |
| **Session persistence** | SQLite / JSONL transcript store                         | You want append-only logs for replay, debugging, audit.           |
| **Subagents**           | Separate agent containers / MCP-routed workers          | Useful later, but do not start here.                              |
| **Observability gap**   | Phoenix + LiteLLM logs + gateway events                 | Phoenix helps, but you need structured gateway events too.        |

The paper’s central point is that the agent loop is simple, while most real engineering lives around it: permissions, context, tools, persistence, extensibility, and recovery. 

## The architecture I’d use for your stack

```text
User / UI
  ↓
Open WebUI or CLI agent
  ↓
Agent runtime
  ↓
LiteLLM Proxy
  ↓
Model providers
  - OpenAI
  - Anthropic
  - local Foundry/Phi
  - Ollama optional

Agent runtime
  ↓
MCP Gateway / Tool Gateway
  ↓
Policy middleware
  ↓
MCP servers
  - filesystem
  - shell
  - git
  - docs/search
  - browser
  - enterprise APIs

All events
  ↓
Observability
  - Phoenix traces
  - SQLite/JSONL transcripts
  - gateway audit log
```

## What LiteLLM is, and is not

LiteLLM maps to the **model routing layer**, not the whole agent platform.

It should own:

* model aliases
* provider routing
* API keys
* fallbacks
* budgets
* request/response logging
* maybe prompt/response capture

It should **not** own:

* tool authorization
* MCP lifecycle
* per-tool permissions
* context compaction
* session memory
* enterprise policy
* human approval workflow

That belongs in a **gateway/control-plane layer**.

## What MCP is in this design

MCP is your **tool integration contract**.

Use it for:

* exposing capabilities
* standardizing tool calls
* avoiding one-off Python scripts per app
* making tools discoverable
* letting multiple agents share the same tool surface

But the paper makes a key point: MCP tools are context-expensive because schemas consume context. Claude Code solves this with layered extensibility: hooks are zero-context, skills are low-context, MCP tools are high-context. 

So your pattern should be:

```text
Hooks first for policy
Skills next for reusable behavior
MCP only when the model needs a real callable tool
Plugins/manifests to package everything
```

## The missing piece: a real agent gateway

This is where your thinking is headed.

Your gateway should sit between agent runtimes and MCP tools:

```text
Agent → Tool Gateway → Policy → MCP Server
```

The gateway should provide:

* identity: who is asking?
* session: what task is this part of?
* policy: is this tool allowed?
* risk tier: read, write, shell, network, destructive
* approval: auto, ask, deny
* sandbox routing: local, container, remote
* audit: what happened and why?
* observability: trace every tool call

This maps directly to the paper’s contrast between Claude Code and OpenClaw: Claude Code puts safety around each tool action, while OpenClaw-style systems put more of the control plane at the gateway perimeter. 

## Recommended build sequence

### 1. Keep LiteLLM simple

Start with:

```yaml
models:
  - openai:gpt-5.5
  - anthropic:claude
  - foundry:phi-local
```

Use it as the model router only.

### 2. Build a thin MCP gateway

Not a full platform. Just:

```text
/mcp/tools
/mcp/call
/policy/check
/audit/events
```

Back it with SQLite.

### 3. Add policy tiers

Use simple rules:

```yaml
tools:
  filesystem.read: allow
  filesystem.write: ask
  shell.npm_test: allow
  shell.rm: deny
  network.fetch: ask
```

This mirrors Claude Code’s deny-first posture without copying all its complexity.

### 4. Add transcript logging

Append-only JSONL:

```json
{"ts":"...","session":"...","actor":"agent","event":"tool.request","tool":"shell","input":"npm test"}
{"ts":"...","session":"...","event":"policy.decision","decision":"allow","reason":"safe test command"}
{"ts":"...","session":"...","event":"tool.result","status":"ok"}
```

This becomes your replay/debug/audit substrate.

### 5. Add Phoenix after the event model is clean

Phoenix should observe the system. It should not be the system.

## My blunt take

Your target architecture is **not just LiteLLM + MCP**.

It is:

```text
LiteLLM = model gateway
MCP = tool protocol
Gateway = control plane
Phoenix = observability
SQLite/JSONL = durable audit/session store
Docker profiles = deployment personas
```

That gives you the Claude Code strengths without locking you into a Claude Code-style CLI-only worldview.
