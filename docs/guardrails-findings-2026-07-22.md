# Guardrails & Governance — Findings and Recommendations

> **Status: Option A implemented (2026-07-22).** DLP (Presidio) + prompt-injection
> heuristic are now available behind the opt-in `guardrails` Compose profile,
> enforced at the LiteLLM gateway (see `docs/deployment-seams.md` § "Guardrails
> / DLP"). Options B (LLM-based injection rail) and C (policy PDP + tool-gating)
> below remain investigation-only / not yet implemented.
>
> This doc lays out a reference **governance plane** design — five swappable
> seams that close the most common "agentic AI" gaps — and recommends what to
> adopt in ai-workbench and in what order.

---

## TL;DR

A useful mental model for agentic-AI governance is **five swappable seams**
wired into one request path, each bound to a stable contract rather than a
specific implementation:

```
  S1  AI Gateway        OpenAI-compatible /v1 API
  S2  Guardrails / DLP  PII/PHI redaction + prompt-injection defense
  S3  Policy PDP        allow/deny decisions for proposed actions
  S4  Tool-gate         allowlisted tool calls, no direct DB/shell
  S5  Observability     OTEL spans for every hop
```

ai-workbench today implements **2 of the 5**:

| Seam | Capability | Reference fill | ai-workbench today | Gap |
|------|-----------|-----------------|--------------------|-----|
| **S1** AI Gateway | OpenAI `/v1` | LiteLLM | ✅ LiteLLM | — |
| **S2** Guardrails/DLP | PII/PHI redaction + prompt-injection | Presidio + LLM-based rail | ✅ Presidio + heuristic (this change) | — |
| **S3** Policy PDP | allow/deny actions | Open Policy Agent (Rego) | ❌ none | **MISSING** |
| **S4** Tool-gate | tool allowlist, no direct DB/shell | allowlist shim / gateway | ⚠️ `mcpo` bridges MCP→REST but **no allowlist/policy** | **PARTIAL** |
| **S5** Observability | OTEL spans | Phoenix | ✅ Phoenix | — |

The three commonly-cited "MUST-have" gaps in agentic AI deployments are **DLP**,
**execution control**, and **tool-gating** — exactly S2, S3, and S4.

---

## The reference governance-plane design

A single request runs through all five seams and returns a per-seam trace so
you can *see* governance happen:

```
POST /chat
  → [S2] injection check   heuristic (offline) + optional LLM-based rail
  → [S2] DLP                redact PII/PHI BEFORE the model
  → [S1] gateway /v1        model inference
  → [S3] policy             allow / deny the proposed tool action
  → [S4] tool-gate          allowlisted tool call, no direct DB/shell
  → [S2] DLP                re-scan model + tool output for leakage
  … [S5] observability      every hop as an OTEL span
```

Key property: **it runs with no model configured.** Seams S2–S5 work fully
offline; only S1 needs real model credentials. That makes the pattern cheap
and safe to build out incrementally.

### Seam-by-seam detail

**S2 — Guardrails / DLP (two layers):**
- *Prompt-injection / jailbreak:* an always-on regex **heuristic** (patterns
  like "ignore previous instructions", "DAN", "developer mode", "reveal system
  prompt") plus an optional LLM-based self-check rail. Either firing refuses
  the request before the model.
- *PII/PHI DLP:* **Microsoft Presidio** (`presidio-analyzer` + `presidio-anonymizer`)
  masks detected entities *before* inference, then re-scans model + tool output
  for leakage. Two lightweight, prebuilt MS containers — no model required.

**S3 — Policy-as-code:** a small Rego policy (Open Policy Agent) returning
`{allow, deny_reason}` for a proposed action. Enforces a tool allowlist, a
**step budget** (kills runaway agent loops), and a **risk threshold** (route
high-risk actions to a human instead of auto-executing).

**S4 — Tool-gate:** a minimal allowlist shim in front of tool execution.
Fail-closed: only allowlisted tools run, everything else is denied. Every call
is audited. The production-grade version of this is a dedicated MCP gateway
(agentgateway-style) rather than a bespoke shim.

**Contract-first design:** each seam is bound to its *contract* (URL + decision
shape), not its implementation — so the policy engine can later become a
vendor ABAC/Purview/Immuta product, DLP can move to a cloud content-safety
service, and the tool-gate can become a full MCP gateway, all without touching
neighbours. This is the same swap-at-the-seam principle ai-workbench already
documents in `docs/deployment-seams.md`.

---

## Recommendations for ai-workbench

Ordered by value-per-effort. All are additive and should be **opt-in via a
Compose profile** (e.g. `guardrails`) to preserve the lean default posture, and
wired through `bootstrap.py` + `.env` like every other service here.

### 1. Presidio DLP (S2) — highest value, lowest effort ⭐ (done)

- **Why:** PII/PHI redaction is the most common hard gap and the most
  defensible "why we need guardrails" story, especially for regulated data.
  Two prebuilt MS containers, no model required, works offline.
- **How:** `presidio-analyzer` + `presidio-anonymizer` on the `agents` network
  under the `guardrails` profile, enforced via a **native LiteLLM guardrail**
  (`guardrail: presidio`, `mode: pre_call`) so redaction happens at the gateway
  for *all* frontends (Open WebUI *and* Onyx), not inside a bespoke agent.
- **Effort:** low (containers) + medium (LiteLLM guardrail wiring). **Implemented.**

### 2. Prompt-injection heuristic (S2) — trivial, immediate (done)

- **Why:** the always-on regex detector needs no model, no new container, and
  blocks the obvious attacks. Cheap defense-in-depth.
- **How:** a custom LiteLLM guardrail (`config/litellm/guardrails/injection_guardrail.py`)
  running `pre_call`. An LLM-backed rail (e.g. NeMo Guardrails or similar) could
  be added later as an optional profile since it needs a model and adds
  latency/cost.
- **Effort:** trivial (heuristic, implemented) / medium (LLM-backed rail, future).

### 3. Policy PDP (S3) — execution control

- **Why:** step-budget + risk-threshold + allowlist is real "execution
  control" and stops runaway agent loops — increasingly relevant as this stack
  grows agent capabilities.
- **How:** add an Open Policy Agent container (`guardrails` profile) serving a
  Rego policy from `config/opa/`. The decision to make is **where it's
  enforced** — most natural is in front of `mcpo` (see #4), since that's where
  tool actions originate.
- **Effort:** low (container) + medium (enforcement wiring). Rego authoring is
  the learning cost.

### 4. Tool-gating for `mcpo` (S4) — close the partial gap

- **Why:** ai-workbench already routes MCP tools through `mcpo`, but there is
  **no allowlist or policy check** — any tool `mcpo` exposes is callable.
  Fail-closed allowlisting + audit is the missing control.
- **How:** two paths —
  1. *Lightweight now:* a thin allowlist/audit shim in front of `mcpo`,
     optionally calling the policy PDP (#3) for the decision.
  2. *Production-grade later:* a dedicated MCP gateway — already spiked in
     `docs/spike-agentgateway-proxy.md`. Worth revisiting that spike alongside
     this.
- **Effort:** low (shim) / higher (dedicated gateway).

### 5. Reuse Phoenix as the governance audit sink (S5)

- **Why:** ai-workbench already runs Phoenix and LiteLLM already emits OTEL.
  Guardrail decisions (redaction counts, policy denials, injection blocks)
  should become **span attributes / events** so the existing `make stats` and
  Phoenix UI show governance activity for free.
- **Effort:** low — mostly instrumentation on the new hooks.

---

## Caveats & open questions

- **Enforcement point is the key design decision.** ai-workbench has multiple
  frontends (Open WebUI, Onyx) hitting LiteLLM directly, with no central
  custom agent. Guardrails should live at the **LiteLLM gateway** (native/custom
  guardrails) and at the **mcpo/tool boundary**, not in a new agent.
- **`config/litellm/config.yaml` is generated** — any LiteLLM guardrail config
  must be added to `bootstrap.py` (the `litellm_settings`/`guardrails` blocks),
  not hand-edited.
- **Latency/cost:** Presidio adds per-request latency; an LLM-based injection
  rail would add model calls too. Keep these behind opt-in profiles so the
  lean daily-driver posture is unaffected.
- **Proxy isolation:** new services must be added to `WORKBENCH_NO_PROXY` or
  inter-service calls will hit the corporate proxy (per repo conventions).
- **Memory budget:** every service here has `deploy.resources.limits.memory`.
  Presidio (spaCy models) and OPA need limits set.
- **Identity is deliberately NOT a seam** in this design — use your identity
  provider (Entra ID / managed identity, etc.) from day one rather than
  bridging identity with OSS components.

## Suggested next step

Option A (Presidio DLP + injection heuristic, enforced at the LiteLLM gateway)
is implemented. That closes the highest-value gap for all frontends at once
and proves the enforcement-at-the-gateway pattern. Next candidates, in order:
a policy PDP (S3, execution control) and tool-gating in front of `mcpo` (S4).
