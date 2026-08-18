# Full-Stack MCP Integration Demo

A repeatable scenario that exercises the entire AI Workbench tool chain:
file upload → multi-agent orchestration → data enrichment → code execution → downloadable output.

## What it exercises

Two paths are available:

### Path A — Code-driven via mcpo (deterministic, recommended)

```
You (CSV data in prompt) → Open WebUI
                             → Code Agent
                                 → writes Python code
                                 → Code Sandbox executes it:
                                     → pd.read_csv() (parse data)
                                     → mcpo REST → Directory MCP (org directory)
                                     → mcpo REST → Ticketing MCP (ticketing data)
                                     → df.to_excel() (export)
                             → Caddy (serves downloadable file)
Phoenix traces the entire chain end-to-end.
```

### Path B — Agent-orchestrated via enrichment agent (exploratory)

```
You (CSV data in prompt) → Open WebUI
                             → Enrichment Agent (orchestrator)
                                 → Directory Agent → Directory MCP (org directory)
                                 → Ticketing Agent → Ticketing MCP (ticketing data)
                                 → Code Sandbox (pandas join + Excel export)
                             → Caddy (serves downloadable file)
Phoenix traces the entire chain end-to-end.
```

**Services touched (Path A — code-driven):** Open WebUI, LiteLLM, Code Agent,
Code Sandbox, mcpo, Directory MCP, Ticketing MCP, Caddy, Phoenix.

**Services touched (Path B — agent-orchestrated):** Open WebUI, LiteLLM, Enrichment Agent, Directory Agent, Directory MCP,
Ticketing Agent, Ticketing MCP, Code Agent (sandbox), Caddy, Phoenix.

## Prerequisites

- Stack is running: `make up` (tools profile)
- Directory credentials set: `DIRECTORY_BIND_DN`, `DIRECTORY_PASSWORD` in `.env`
- the ticketing system credentials set: `TICKETING_USERNAME`, `TICKETING_PASSWORD` in `.env`
- Verify health: `make health`

## Scenario A — Code-driven via mcpo (recommended)

This path has the LLM write Python code that calls enterprise APIs directly
via mcpo REST endpoints. Everything after code generation is deterministic.

### Step 1 — Run the scenario in Open WebUI

1. Open **http://chat.localhost** (or http://localhost:3000)
2. Start a **new chat**
3. Click the **wrench/tools icon** and enable the **Code Agent**
4. Send this prompt (replace the sample IDs with real ones):

> Here are some team members:
>
> ```
> login,role
> jdoe,lead
> ```
>
> Write Python code that:
> 1. Parses this CSV data
> 2. For each login, calls the directory REST API to get their display name,
>    title, department, and manager
> 3. For each login, queries the ticketing system for open incidents assigned to them
> 4. Merges everything into an Excel file with two sheets:
>    "Team Profile" and "Open Incidents"
>
> Use httpx to call the mcpo gateway at http://mcpo:8000.
> Check http://mcpo:8000/directory/openapi.json and
> http://mcpo:8000/ticketing/openapi.json for the exact endpoints and parameters.

### Step 2 — What to watch for

| Phase | What happens |
|-------|--------------|
| Planning | Code Agent describes its approach |
| Code generation | Writes Python using pandas + httpx + mcpo |
| Execution | Sandbox runs the code — calls mcpo → directory/ticketing → gets data |
| Result | Returns download link for the Excel file |

### Expected output

A clickable download link:

```
http://localhost:8090/sandbox/files/<job_id>/team_report.xlsx
```

### Step 3 — Verify in Phoenix

Open **http://phoenix.localhost** and inspect:

| Phoenix project | What to look for |
|----------------|-------------------|
| `code-agent` | Top-level span with `create_job`, `execute_code` tool calls |
| `litellm-proxy` | LLM API calls for code generation |

---

## Scenario B — Agent-orchestrated via enrichment agent

This path uses the enrichment agent to orchestrate specialist agents.
More flexible for open-ended analysis but involves more LLM calls.

### Step 1 — Prepare the input data

Have 3–5 real login IDs (username values) from your org ready.
You'll paste them directly into the prompt.

> **Why not upload a file?** Open WebUI injects uploaded files into chat context
> via RAG, but that context doesn't get forwarded to MCP tool calls. Pasting
> the data inline ensures the enrichment agent receives it.

You can verify login IDs exist first by asking the Directory Agent directly:
*"Look up user jdoe"*

### Step 2 — Run the scenario in Open WebUI

1. Open **http://chat.localhost** (or http://localhost:3000)
2. Start a **new chat**
3. Click the **wrench/tools icon** and enable the **Enrichment Agent**
4. Send this prompt (replace the sample IDs with real ones):

> Here is my team roster:
>
> ```
> login,role
> jdoe,lead
> ```
>
> For each person:
> 1. Look up their display name, title, department, and manager in the directory
> 2. Find any open incidents assigned to them in the ticketing system
> 3. Combine everything into a single Excel file with two sheets:
>    "Team Profile" (roster + directory data) and "Open Incidents"
>    (all incidents with the assignee's display name).
>    Include a download link for the file.

### Step 3 — What to watch for

The enrichment agent works through the task in phases. You should see it
narrate its plan and then execute each step:

| Phase | Agent called | What happens |
|-------|-------------|--------------|
| Plan | Enrichment Agent | Reads CSV, describes its approach |
| Directory lookup | → Directory Agent | Queries the directory for each login — returns names, titles, departments, managers |
| Ticketing query | → Ticketing Agent | Searches for open incidents assigned to each person |
| Sandbox setup | → Code Sandbox | Creates a job, uploads the gathered data as CSV |
| Code execution | → Code Sandbox | Runs Python/pandas to merge data and write an Excel file |
| Result | Enrichment Agent | Returns a download link for the Excel file |

### Expected output

The agent should provide a clickable download link in the form:

```
http://localhost:8090/sandbox/files/<job_id>/team_report.xlsx
```

The Excel file should contain:

- **Sheet "Team Profile"** — login, role, displayName, title, department, manager
- **Sheet "Open Incidents"** — incident number, short description, state, assignee display name

### Step 4 — Verify the trace in Phoenix

Open **http://phoenix.localhost** and inspect:

| Phoenix project | What to look for |
|----------------|-------------------|
| `enrichment-agent` | Top-level span with nested `ask_directory`, `ask_ticketing`, `create_job`, `upload_file`, `execute_code` tool calls |
| `directory-agent` | `find_user` tool calls (one per login ID) |
| `ticketing-agent` | `query_table` calls against the `incident` table |
| `litellm-proxy` | All LLM API calls from every agent |

You can also run:

```bash
make stats
```

This queries Phoenix and prints tool call counts, error rates, and latency
percentiles for each agent.

## Success criteria

- [ ] Enrichment agent narrates a clear plan before executing
- [ ] directory data returned for all roster members
- [ ] the ticketing system incidents (or "no open incidents") returned per person
- [ ] Excel file downloads and opens with both sheets populated
- [ ] Phoenix shows spans across all four agent projects
- [ ] `make stats` reflects the tool calls from the run

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| "I don't have access to tools" | Enrichment Agent not enabled in chat | Click wrench icon, toggle it on |
| the directory returns empty results | Bad credentials or login ID not found | Check `DIRECTORY_BIND_DN`/`DIRECTORY_PASSWORD` in `.env`; test with Directory Agent directly |
| the ticketing system returns empty | Bad credentials or no matching incidents | Check `TICKETING_USERNAME`/`TICKETING_PASSWORD`; try a broader query |
| Download link returns 404 | Caddy not routing sandbox files | Check `make logs` for caddy; verify Open Terminal is healthy |
| Code execution timeout | Complex script exceeding 30s default | Check Open Terminal logs; enrichment agent should retry with simpler code |
| Agent loops or stalls | LLM producing malformed tool calls | Check the agent's Phoenix trace for the error; restart the agent container |

## Variations

Once the basic scenario works, try these to exercise more of the stack:

### Variation A — Chart output
Add to the prompt: *"Also create a bar chart showing open incident count per team member and include it as a PNG."*
This exercises matplotlib in the sandbox.

### Variation B — Manager chain
Change the prompt: *"For each person, also get their full management chain up to VP level."*
This exercises the directory agent's `get_manager_chain` tool.

### Variation C — the ticketing system catalog requests
Change the prompt: *"Instead of incidents, find any catalog requests (RITMs) opened by each person in the last 90 days."*
This exercises the ticketing agent's `search_catalog_requests` tool.

### Variation D — Code Agent only (no enrichment)
Skip the enrichment agent. Enable the **Code Agent** tool instead, upload a pre-made
CSV, and ask: *"Read the uploaded CSV and create a summary Excel with pivot tables."*
This isolates the code sandbox path.
