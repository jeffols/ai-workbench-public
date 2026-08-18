#!/usr/bin/env python3
"""Health checks for the AI Workbench.

Discovers MCP tools from mcp-tool.yaml manifests and checks each one
through the Caddy reverse proxy, alongside the standard platform services.
"""
from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
SERVICES_DIR = ROOT / "services"

LITELLM_BASE = "http://127.0.0.1:4000"
CADDY_BASE = "http://127.0.0.1:8090"

PASS = "OK  "
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"


def _is_connection_refused(exc: Exception) -> bool:
    """Return True when the error means the service is simply not running."""
    msg = str(exc).lower()
    return "connection refused" in msg or "urlopen error" in msg and "refused" in msg


def parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def discover_tools() -> list[dict[str, Any]]:
    """Find all mcp-tool.yaml manifests under services/."""
    tools: list[dict[str, Any]] = []
    if not SERVICES_DIR.is_dir():
        return tools
    for manifest in sorted(SERVICES_DIR.glob("*/mcp-tool.yaml")):
        try:
            import yaml
            tool = yaml.safe_load(manifest.read_text()) or {}
        except ImportError:
            # Minimal YAML parser for simple key: value files
            import re
            tool = {}
            for line in manifest.read_text().splitlines():
                line = line.split("#")[0]
                if not line.strip() or line.startswith(" "):
                    continue
                m = re.match(r"^(\w[\w_-]*):\s*(.*)", line)
                if m:
                    key, val = m.group(1), m.group(2).strip()
                    if val.startswith("[") and val.endswith("]"):
                        tool[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
                    elif val.isdigit():
                        tool[key] = int(val)
                    else:
                        tool[key] = val.strip("'\"")
        if tool.get("slug"):
            tools.append(tool)
    return tools


def _get(url: str, headers: dict[str, str] | None = None, timeout: int = 10) -> tuple[int, object]:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def _post(url: str, body: dict, headers: dict[str, str] | None = None, timeout: int = 30) -> tuple[int, object]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def check_openwebui(env: dict) -> bool:
    port = env.get("OPENWEBUI_PORT", "3000")
    url = f"http://127.0.0.1:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            print(f"{PASS} openwebui          status={resp.status}")
            return True
    except Exception as exc:
        print(f"{FAIL} openwebui          error={exc}")
        return False


def check_caddy() -> bool:
    url = f"{CADDY_BASE}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            print(f"{PASS} caddy              status={resp.status}")
            return True
    except Exception as exc:
        print(f"{FAIL} caddy              error={exc}")
        return False


def check_litellm_live() -> bool:
    url = f"{LITELLM_BASE}/health/liveliness"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            print(f"{PASS} litellm/liveliness status={resp.status}")
            return True
    except Exception as exc:
        print(f"{FAIL} litellm/liveliness error={exc}")
        print("       hint: make sure docker compose is up and LiteLLM is listening on 127.0.0.1:4000")
        return False


def check_litellm_models(auth: str) -> list[str]:
    try:
        _, body = _get(f"{LITELLM_BASE}/models", headers={"Authorization": auth})
        models = [m["id"] for m in body.get("data", [])]  # type: ignore[union-attr]
        print(f"{PASS} litellm/models     {models}")
        return models
    except Exception as exc:
        print(f"{FAIL} litellm/models     error={exc}")
        return []


def check_litellm_completion(auth: str, model: str) -> bool:
    try:
        _, body = _post(
            f"{LITELLM_BASE}/v1/chat/completions",
            body={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                "max_tokens": 5,
            },
            headers={"Authorization": auth},
        )
        reply = body["choices"][0]["message"]["content"].strip()  # type: ignore[index]
        print(f"{PASS} litellm/completion model={model} reply={reply!r}")
        return True
    except Exception as exc:
        print(f"{FAIL} litellm/completion model={model} error={exc}")
        return False


def check_open_notebook(env: dict) -> bool | None:
    """Check Open Notebook.  Returns None (skip) when the service is not running."""
    port = env.get("OPEN_NOTEBOOK_PORT", "8502")
    url = f"http://127.0.0.1:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            print(f"{PASS} open-notebook      status={resp.status}")
            return True
    except Exception as exc:
        if _is_connection_refused(exc):
            print(f"{SKIP} open-notebook      not running (notebook profile not active?)")
            return None
        print(f"{FAIL} open-notebook      error={exc}")
        return False


def check_mcpo() -> bool | None:
    """Check mcpo OpenAPI proxy.  Returns None (skip) when not running."""
    url = f"{CADDY_BASE}/mcpo/fetch/docs"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            print(f"{PASS} mcpo               status={resp.status} via caddy")
            return True
    except Exception as exc:
        if _is_connection_refused(exc):
            print(f"{SKIP} mcpo               not running (tools profile not active?)")
            return None
        print(f"{FAIL} mcpo               error={exc}")
        return False


def check_mcp_tool_via_caddy(tool: dict[str, Any]) -> bool:
    """Check an MCP tool's health endpoint through Caddy."""
    slug = tool["slug"]
    health_path = tool.get("health_path", "/healthz")
    url = f"{CADDY_BASE}/mcp/{slug}{health_path}"
    label = f"mcp/{slug}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            print(f"{PASS} {label:<18s} status={resp.status} via caddy")
            return True
    except urllib.error.HTTPError as exc:
        print(f"{FAIL} {label:<18s} HTTP {exc.code} at {url}")
    except Exception as exc:
        print(f"{FAIL} {label:<18s} error={exc}")
    return False


def check_mcp_oreilly(auth_header: str) -> bool:
    """Verify O'Reilly MCP server is reachable and returns tools directly."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    req = urllib.request.Request(
        "https://api.oreilly.com/api/content-discovery/v1/mcp/",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": auth_header,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            tools = body.get("result", {}).get("tools", [])
            if tools:
                names = [t.get("name", "?") for t in tools]
                print(f"{PASS} mcp/oreilly        {len(tools)} tools: {', '.join(names)}")
                return True
            print(f"{WARN} mcp/oreilly        connected but no tools returned")
            return False
    except urllib.error.HTTPError as exc:
        print(f"{FAIL} mcp/oreilly        HTTP {exc.code}")
    except Exception as exc:
        print(f"{FAIL} mcp/oreilly        error={exc}")
    return False


def main() -> None:
    if not ENV_FILE.exists():
        print("missing .env; run python3 scripts/bootstrap.py first")
        sys.exit(1)

    env = parse_env(ENV_FILE)
    master_key = env.get("LITELLM_MASTER_KEY", "")
    default_model = env.get("DEFAULT_CHAT_MODEL", "openai-fast")

    failures = 0

    # Platform core
    print("── Platform ──")
    if not check_caddy():
        failures += 1
    if not check_openwebui(env):
        failures += 1
    notebook_result = check_open_notebook(env)
    if notebook_result is False:
        failures += 1
    if not check_litellm_live():
        failures += 1
    elif master_key:
        auth = f"Bearer {master_key}"
        models = check_litellm_models(auth)
        if not models:
            failures += 1
        chat_model = default_model if default_model in models else (models[0] if models else None)
        if chat_model:
            if not check_litellm_completion(auth, chat_model):
                failures += 1
        else:
            print(f"{WARN} litellm/completion skipped — no models available")
    else:
        print(f"{WARN} litellm            LITELLM_MASTER_KEY not set, skipping auth checks")

    # MCP tools (discovered from manifests, checked via Caddy)
    tools = discover_tools()
    if tools:
        print("\n── MCP Tools (via Caddy) ──")
        for tool in tools:
            if not check_mcp_tool_via_caddy(tool):
                failures += 1

    # mcpo OpenAPI proxy (tools profile)
    mcpo_result = check_mcpo()
    if mcpo_result is False:
        failures += 1

    # External MCP tools
    oreilly_auth = env.get("OREILLY_AUTH_HEADER", "")
    if oreilly_auth:
        print("\n── External MCP ──")
        if not check_mcp_oreilly(oreilly_auth):
            failures += 1

    if failures:
        print(f"\n{failures} check(s) failed")
        sys.exit(1)
    else:
        print("\nall checks passed")


if __name__ == "__main__":
    main()
