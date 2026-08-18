"""Code Agent MCP Server.

Exposes an ``execute_task`` tool that accomplishes data processing tasks by
writing and executing Python code in an isolated sandbox.  Uses a ReAct-style
agentic loop with LiteLLM for reasoning and the Open Terminal HTTP API for
execution.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import inject as otel_inject
from opentelemetry.trace import StatusCode

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "openai-strong")
SANDBOX_URL = os.environ.get("CODE_SANDBOX_URL", "http://open-terminal:8000")
TERMINAL_API_KEY = os.environ.get("TERMINAL_API_KEY", "")
# Public base URL for file downloads — served via Caddy
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8090")
MCPO_URL = os.environ.get("MCPO_URL", "http://mcpo:8000")
DISCOVERY_JSON_PATH = os.environ.get(
    "WORKBENCH_TOOLS_JSON", "/opt/workbench/tools.json"
)

MAX_STEPS = 20          # code tasks are iterative — more steps than query agents
_BODY_LOG_LIMIT = 4000  # truncate logged bodies

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("code-agent")

# ── OpenTelemetry ──────────────────────────────────────────────────────────

_otel_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
if _otel_endpoint:
    _resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "code-agent"),
        "openinference.project.name": os.environ.get("PHOENIX_PROJECT_NAME", "code-agent"),
    })
    _provider = TracerProvider(resource=_resource)
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_provider)
    log.info("OTEL tracing enabled → %s", _otel_endpoint)
else:
    log.info("OTEL tracing disabled (OTEL_EXPORTER_OTLP_ENDPOINT not set)")

tracer = trace.get_tracer("code-agent")

# ── Dynamic tool discovery ─────────────────────────────────────────────────

import time as _time
from pathlib import Path as _Path

_discovery_cache: dict | None = None
_discovery_ts: float = 0.0
_DISCOVERY_TTL = 300  # seconds


def _load_static_catalog() -> dict:
    """Load the bootstrap-generated workbench-tools.json."""
    try:
        return json.loads(_Path(DISCOVERY_JSON_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _fetch_mcpo_tools(server: str) -> list[dict]:
    """Fetch tool list from mcpo OpenAPI spec for a server."""
    try:
        resp = httpx.get(f"{MCPO_URL}/{server}/openapi.json", timeout=10)
        resp.raise_for_status()
        spec = resp.json()
    except Exception as exc:
        log.warning("Failed to fetch mcpo tools for %s: %s", server, exc)
        return []

    schemas = spec.get("components", {}).get("schemas", {})
    tools = []
    for path, methods in spec.get("paths", {}).items():
        for method, detail in methods.items():
            tool_name = path.lstrip("/")
            params = {}
            body = detail.get("requestBody", {})
            content = body.get("content", {}).get("application/json", {})
            schema = content.get("schema", {})
            if "$ref" in schema:
                ref_name = schema["$ref"].rsplit("/", 1)[-1]
                schema = schemas.get(ref_name, schema)
            if schema.get("properties"):
                required = set(schema.get("required", []))
                params = {
                    k: {
                        "type": v.get("type", "string"),
                        "required": k in required,
                    }
                    for k, v in schema["properties"].items()
                }
            tools.append({
                "name": tool_name,
                "description": detail.get("summary", ""),
                "parameters": params,
            })
    return tools


def _discover_data_sources() -> dict[str, Any]:
    """Discover mcpo-proxied data sources with live tool details.

    Lazy-loaded with TTL cache. Returns stale cache on failure.
    """
    global _discovery_cache, _discovery_ts

    now = _time.time()
    if _discovery_cache and (now - _discovery_ts) < _DISCOVERY_TTL:
        return _discovery_cache

    catalog = _load_static_catalog()
    mcpo_servers = catalog.get("mcpo_servers", {})

    result = {}
    for name, info in mcpo_servers.items():
        tools = _fetch_mcpo_tools(name)
        result[name] = {
            "description": info.get("description", ""),
            "tools": tools,
        }

    if result:
        _discovery_cache = result
        _discovery_ts = now
        log.info("Discovered %d mcpo data sources: %s", len(result), list(result.keys()))
    elif _discovery_cache:
        log.warning("Live mcpo discovery failed; using stale cache")
        return _discovery_cache
    else:
        log.warning("No mcpo data sources discovered — check mcpo and registry connectivity")

    return result


def _build_data_sources_prompt() -> str:
    """Build the 'Workbench Data Sources' system prompt section dynamically."""
    sources = _discover_data_sources()
    if not sources:
        return (
            "## Workbench Data Sources\n\n"
            "No data sources are currently available. The mcpo gateway may be offline.\n"
            "Check `http://mcpo:8000/<server>/docs` for available endpoints.\n"
        )

    lines = [
        "## Workbench Data Sources (via mcpo REST gateway)",
        "",
        "The sandbox can query enterprise data sources via REST. The mcpo gateway",
        f"at `{MCPO_URL}` proxies MCP tools as standard OpenAPI endpoints.",
        "",
    ]

    for server_name, info in sources.items():
        desc = info.get("description", server_name)
        lines.append(f"### {server_name.upper().replace('-', ' ')}")
        lines.append(f"- **Description:** {desc}")
        lines.append(f"- **Endpoint:** `POST {MCPO_URL}/{server_name}/<tool_name>`")
        lines.append(f"- **Docs:** `{MCPO_URL}/{server_name}/docs`")

        tools = info.get("tools", [])
        if tools:
            tool_names = ", ".join(t["name"] for t in tools)
            lines.append(f"- **Tools:** {tool_names}")
            # Show parameter info for first tool as an example
            example_tool = tools[0]
            params = example_tool.get("parameters", {})
            if params:
                param_parts = []
                for pname, pinfo in params.items():
                    req = " (required)" if pinfo.get("required") else ""
                    param_parts.append(f'"{pname}": "<{pinfo.get("type", "string")}>"{req}')
                lines.append(f"- Example: `httpx.post(\"{MCPO_URL}/{server_name}/{example_tool['name']}\", "
                             f'json={{{", ".join(param_parts[:3])}}}, timeout=30)`')
        lines.append("")

    lines.extend([
        "**Always check the OpenAPI docs first** "
        f"(`httpx.get('{MCPO_URL}/<server>/openapi.json')`)",
        "to discover exact parameter names and response shapes.",
        "",
        "**When the task involves enriching data with enterprise information,",
        "write code that calls mcpo directly rather than asking the user to provide it.**",
        "This is deterministic and scales to any number of rows.",
        "",
        "### Discovery module (pre-installed in sandbox)",
        "",
        "The sandbox has a `discovery` module for programmatic tool discovery:",
        "```python",
        "import discovery",
        "",
        "# List all available data sources and their tools",
        "sources = discovery.discover()",
        "for name, info in sources.items():",
        '    print(f"{name}: {info[\'description\']}")',
        '    for tool in info["tools"]:',
        '        print(f"  {tool[\'name\']}: {tool[\'description\']}")',
        "",
        "# Call a tool directly",
        'result = discovery.call_tool("fetch", "fetch", url="https://example.com")',
        "```",
    ])

    return "\n".join(lines)


# ── System prompt ──────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    """Build system prompt with dynamically discovered data sources."""
    dl_base = PUBLIC_BASE_URL
    data_sources = _build_data_sources_prompt()
    lines = [
        "You are a code execution agent. You help users accomplish data processing",
        "tasks by writing and running Python code in an isolated sandbox.",
        "",
        "## Available tools",
        "",
        "**CRITICAL: You must ALWAYS respond with EXACTLY one JSON object. No exceptions.**",
        "",
        "To call a tool:",
        '{"tool": "<tool_name>", "args": {<arguments>}}',
        "",
        "When you have enough information to answer:",
        '{"answer": "<your final answer in markdown>"}',
        "",
        "NEVER explain what you would do — just DO IT by calling the tool.",
        "",
        "### Tool catalog",
        "",
        "1. **create_job** — Create a new sandbox workspace. Call this FIRST.",
        "   Args: (none)",
        '   Returns: {"job_id": "..."} — use this job_id for all subsequent calls.',
        "",
        "2. **upload_file** — Write a file into the job workspace.",
        "   Args: `job_id` (str, required), `filename` (str, required), `content` (str, required)",
        "   Use this to stage input data (CSVs, text files) before running code.",
        "",
        "3. **execute_code** — Run Python code in the sandbox.",
        "   Args: `job_id` (str, required), `code` (str, required), `timeout` (int, optional, default 30, max 120)",
        '   Returns: {"stdout": "...", "stderr": "...", "exit_code": 0, "files": [...]}',
        "   The code runs in `/home/user/jobs/<job_id>/` — read inputs and write outputs there.",
        "",
        "4. **list_files** — List files in the job workspace.",
        "   Args: `job_id` (str, required)",
        "",
        "5. **read_file** — Read a file from the job workspace (first 50 lines).",
        "   Args: `job_id` (str, required), `filename` (str, required), `max_lines` (int, optional, default 50)",
        "",
        "6. **magic_fetch** — Use when you need data you do not have access to.",
        "   Args: `description` (str, required)",
        "   Logs your request for capability planning. Does not return real data.",
        "",
        "## Workflow",
        "",
        "1. Call `create_job` to get a workspace",
        "2. If the user provided data (CSV, text, etc.), call `upload_file` to stage it",
        "3. Write Python code to accomplish the task",
        "4. Call `execute_code` to run it",
        "5. If there are errors, read stderr, fix the code, and retry",
        "6. Call `list_files` to see output files created",
        "7. Call `read_file` to preview results",
        "8. Provide the final answer with results and download links",
        "",
        "## File downloads",
        "",
        f"Output files are downloadable at: {dl_base}/<job_id>/<filename>",
        "Always include download links in your final answer for any output files created.",
        "",
        "## Sandbox environment (Open Terminal)",
        "",
        "- **pandas**, **openpyxl**, **xlrd**, **scipy**, **scikit-learn** — data science",
        "- **numpy** — numerical computation",
        "- **matplotlib** — charts (save with `plt.savefig(...)`, never `plt.show()`)",
        "- **httpx** — HTTP client for calling workbench REST APIs",
        "- **Node.js**, **git**, **jq**, **ffmpeg** — general tooling",
        "- Standard library: csv, json, re, pathlib, collections, itertools, etc.",
        "",
        data_sources,
        "",
        "## Rules",
        "",
        "- Always create a job first, then use that job_id for everything.",
        "- Write self-contained Python scripts, not interactive/REPL-style code.",
        "- **ALWAYS upload data as CSV (never xlsx).** You work with text — you",
        "  cannot produce binary Excel files via upload_file. Use `pd.read_csv()`",
        "  to read uploaded data. To CREATE Excel output, use `df.to_excel()`.",
        "- For charts, ALWAYS save to file — never call plt.show().",
        "- Print key results to stdout so you can see them in the execute_code response.",
        "- If code fails, fix it and retry.",
        "- **Always use markdown tables when presenting tabular results.**",
        "- When the user provides data inline, upload it first via upload_file.",
    ]
    return "\n".join(lines)


_system_prompt_cache: str | None = None


def get_system_prompt() -> str:
    """Return the system prompt, building it lazily on first call."""
    global _system_prompt_cache
    if _system_prompt_cache is None:
        _system_prompt_cache = _build_system_prompt()
        log.info("System prompt built (%d chars)", len(_system_prompt_cache))
    return _system_prompt_cache


# ── Sandbox HTTP client ───────────────────────────────────────────────────

async def call_sandbox(
    client: httpx.AsyncClient,
    tool_name: str,
    tool_args: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a tool call to the Open Terminal HTTP API.

    Translates the job-based tool interface to Open Terminal's filesystem +
    execute API while keeping the same contract that the LLM expects.
    """
    auth = {"Authorization": f"Bearer {TERMINAL_API_KEY}"} if TERMINAL_API_KEY else {}
    jobs_root = "/home/user/jobs"

    if tool_name == "create_job":
        import uuid
        job_id = uuid.uuid4().hex[:12]
        resp = await client.post(
            f"{SANDBOX_URL}/files/mkdir",
            json={"path": f"{jobs_root}/{job_id}"},
            headers=auth, timeout=10.0,
        )
        resp.raise_for_status()
        return {"job_id": job_id}

    elif tool_name == "upload_file":
        job_id = tool_args["job_id"]
        resp = await client.post(
            f"{SANDBOX_URL}/files/write",
            json={
                "path": f"{jobs_root}/{job_id}/{tool_args['filename']}",
                "content": tool_args["content"],
            },
            headers=auth, timeout=30.0,
        )
        resp.raise_for_status()
        return {"filename": tool_args["filename"], "size": len(tool_args["content"])}

    elif tool_name == "execute_code":
        job_id = tool_args["job_id"]
        timeout = min(int(tool_args.get("timeout", 30)), 120)
        cwd = f"{jobs_root}/{job_id}"

        # Write code to a script file first
        await client.post(
            f"{SANDBOX_URL}/files/write",
            json={"path": f"{cwd}/_script.py", "content": tool_args["code"]},
            headers=auth, timeout=10.0,
        )

        # Execute with OS-level timeout to enforce kill semantics
        resp = await client.post(
            f"{SANDBOX_URL}/execute",
            json={"command": f"timeout {timeout}s python3 _script.py", "cwd": cwd},
            params={"wait": str(timeout + 5)},
            headers=auth,
            timeout=float(timeout + 15),
        )
        resp.raise_for_status()
        result = resp.json()

        # Convert Open Terminal output format to agent-expected format
        # Open Terminal uses a PTY, so output contains \r\n line endings
        stdout_parts = []
        stderr_parts = []
        for entry in result.get("output", []):
            if entry.get("type") == "stderr":
                stderr_parts.append(entry.get("data", ""))
            else:
                stdout_parts.append(entry.get("data", ""))

        raw_stdout = "".join(stdout_parts).replace("\r\n", "\n")
        raw_stderr = "".join(stderr_parts).replace("\r\n", "\n")

        # List files after execution for the manifest
        files_resp = await client.get(
            f"{SANDBOX_URL}/files/list",
            params={"directory": cwd},
            headers=auth, timeout=10.0,
        )
        files = []
        if files_resp.status_code == 200:
            for entry in files_resp.json().get("entries", []):
                name = entry.get("name", "")
                if not name.startswith("_") and entry.get("type") == "file":
                    files.append({"name": name, "size": entry.get("size", 0)})

        return {
            "stdout": raw_stdout,
            "stderr": raw_stderr,
            "exit_code": result.get("exit_code", -1),
            "files": files,
        }

    elif tool_name == "list_files":
        job_id = tool_args["job_id"]
        resp = await client.get(
            f"{SANDBOX_URL}/files/list",
            params={"directory": f"{jobs_root}/{job_id}"},
            headers=auth, timeout=10.0,
        )
        resp.raise_for_status()
        entries = resp.json().get("entries", [])
        files = [
            {"name": e["name"], "size": e.get("size", 0)}
            for e in entries
            if not e["name"].startswith("_") and e.get("type") == "file"
        ]
        return {"job_id": job_id, "files": files}

    elif tool_name == "read_file":
        job_id = tool_args["job_id"]
        filename = tool_args["filename"]
        resp = await client.get(
            f"{SANDBOX_URL}/files/read",
            params={"path": f"{jobs_root}/{job_id}/{filename}"},
            headers=auth, timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", "")
        # Truncate to max_lines if requested
        max_lines = int(tool_args.get("max_lines", 50))
        lines = content.split("\n")
        if len(lines) > max_lines:
            content = "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"
        return {"filename": filename, "content": content, "truncated": len(lines) > max_lines}

    else:
        return {"error": f"Unknown sandbox tool: {tool_name}"}


# ── LiteLLM chat completions ──────────────────────────────────────────────


async def chat_completion(
    client: httpx.AsyncClient, messages: list[dict[str, str]],
) -> str:
    """Send messages to LiteLLM and return the assistant's reply text."""
    with tracer.start_as_current_span("chat_completion") as span:
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("llm.model_name", AGENT_MODEL)
        span.set_attribute("llm.invocation_parameters",
                           json.dumps({"model": AGENT_MODEL, "temperature": 0.1}))
        span.set_attribute("input.value",
                           json.dumps(messages, default=str)[:_BODY_LOG_LIMIT])

        llm_headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_API_KEY}",
        }
        otel_inject(llm_headers)

        resp = await client.post(
            f"{LITELLM_URL}/v1/chat/completions",
            json={"model": AGENT_MODEL, "messages": messages, "temperature": 0.1},
            headers=llm_headers,
            timeout=120.0,  # code generation can be lengthy
        )
        resp.raise_for_status()
        data = resp.json()
        reply = data["choices"][0]["message"]["content"]

        span.set_attribute("output.value", reply[:_BODY_LOG_LIMIT])
        usage = data.get("usage", {})
        if usage.get("prompt_tokens"):
            span.set_attribute("llm.token_count.prompt", usage["prompt_tokens"])
        if usage.get("completion_tokens"):
            span.set_attribute("llm.token_count.completion", usage["completion_tokens"])
        if usage.get("total_tokens"):
            span.set_attribute("llm.token_count.total", usage["total_tokens"])

        return reply


# ── ReAct agentic loop ────────────────────────────────────────────────────


def _parse_action(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object with a ``tool`` or ``answer`` key."""
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i : j + 1])
                        if isinstance(obj, dict) and ("tool" in obj or "answer" in obj):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
    return None


SANDBOX_TOOLS = {"create_job", "upload_file", "execute_code", "list_files", "read_file"}


async def run_agent(question: str) -> str:
    """Execute the ReAct loop and return the final answer."""
    with tracer.start_as_current_span("code_task") as agent_span:
        agent_span.set_attribute("openinference.span.kind", "AGENT")
        agent_span.set_attribute("input.value", question)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": get_system_prompt()},
            {"role": "user", "content": question},
        ]

        answer: str | None = None

        async with httpx.AsyncClient() as client:
            for step in range(1, MAX_STEPS + 1):
                log.info("Step %d/%d", step, MAX_STEPS)
                with tracer.start_as_current_span("react_step") as step_span:
                    step_span.set_attribute("openinference.span.kind", "CHAIN")
                    step_span.set_attribute("react.step", step)

                    reply = await chat_completion(client, messages)
                    log.info("LLM reply: %s", reply[:300])

                    action = _parse_action(reply)

                    if action is None:
                        # Nudge the LLM back into JSON mode
                        if step < MAX_STEPS:
                            log.warning("No JSON action found — nudging LLM (step %d)", step)
                            messages.append({"role": "assistant", "content": reply})
                            messages.append({
                                "role": "user",
                                "content": (
                                    "You must respond with a JSON object. "
                                    'Either call a tool: {"tool": "<name>", "args": {...}} '
                                    'or give your final answer: {"answer": "..."}. '
                                    "Do NOT explain — act now."
                                ),
                            })
                            continue
                        answer = reply
                        break

                    if "answer" in action:
                        answer = action["answer"]
                        break

                    tool_name = action.get("tool", "")
                    tool_args = action.get("args", {})

                    # magic_fetch — demand-signal collector (handled locally)
                    if tool_name == "magic_fetch":
                        description = tool_args.get("description", "")
                        with tracer.start_as_current_span("magic_fetch") as mf_span:
                            mf_span.set_attribute("openinference.span.kind", "TOOL")
                            mf_span.set_attribute("tool.name", "magic_fetch")
                            mf_span.set_attribute("input.value", description)
                            mf_span.set_attribute("magic_fetch.description", description)
                            mf_span.set_attribute("magic_fetch.source", "code-agent")
                            log.info("magic_fetch: %s", description[:200])
                            result_text = "Data retrieved successfully. Continue your reasoning."
                            mf_span.set_attribute("output.value", result_text)
                        messages.append({"role": "assistant", "content": reply})
                        messages.append({"role": "user", "content": f"Tool result for {tool_name}:\n{result_text}"})
                        continue

                    # Sandbox tools
                    with tracer.start_as_current_span(tool_name) as tool_span:
                        tool_span.set_attribute("openinference.span.kind", "TOOL")
                        tool_span.set_attribute("tool.name", tool_name)
                        tool_span.set_attribute("tool.parameters",
                                                json.dumps(tool_args, default=str)[:_BODY_LOG_LIMIT])
                        tool_span.set_attribute("input.value",
                                                json.dumps(tool_args, default=str)[:_BODY_LOG_LIMIT])
                        try:
                            if tool_name not in SANDBOX_TOOLS:
                                result_text = json.dumps({"error": f"Unknown tool: {tool_name}"})
                            else:
                                result = await call_sandbox(client, tool_name, tool_args)
                                result_text = json.dumps(result, indent=2, default=str)
                            log.info("Sandbox ← %s: %s", tool_name, result_text[:500])
                            tool_span.set_attribute(
                                "output.value", result_text[:_BODY_LOG_LIMIT],
                            )
                        except Exception as exc:
                            result_text = f"Error calling {tool_name}: {exc}"
                            tool_span.set_status(StatusCode.ERROR, result_text)
                            tool_span.record_exception(exc)
                            tool_span.set_attribute("output.value", result_text)
                            log.warning(result_text)

                    messages.append({"role": "assistant", "content": reply})
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Tool result for {tool_name}:\n{result_text}",
                        }
                    )

            if answer is None:
                answer = "I was unable to complete the task within the allowed steps."

            agent_span.set_attribute("output.value", answer[:_BODY_LOG_LIMIT])
            _flush_traces()

        return answer


def _flush_traces():
    """Force-flush buffered spans so Phoenix sees them immediately."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush(timeout_millis=5000)


# ── FastMCP server ─────────────────────────────────────────────────────────

mcp = FastMCP("Code Agent", host="0.0.0.0", port=8080)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    return JSONResponse({"status": "ok", "service": "code-agent"})


@mcp.tool()
async def execute_task(task: str) -> str:
    """Execute a data processing task by writing and running Python code.

    Describe what you need done — include any input data (CSV rows, column
    names, file contents) directly in the task description.  The agent will
    write Python code, execute it in an isolated sandbox with pandas/numpy/
    matplotlib, and return the results.

    Examples:
    - "Parse this CSV and show the top 10 rows by revenue: ..."
    - "Read the attached spreadsheet and create a pivot table of sales by region"
    - "Generate a bar chart of department headcounts from this data: ..."
    """
    log.info("execute_task called (%d chars)", len(task))
    return await run_agent(task)


# ── Entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting Code Agent MCP server on :8080")
    mcp.run(transport="streamable-http")
