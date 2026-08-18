"""Workbench tool discovery via the mcpo REST gateway and agentregistry.

Usage from the Open Terminal sandbox::

    import discovery

    # List all mcpo-proxied data sources and their tools
    catalog = discovery.discover()
    for name, info in catalog.items():
        print(f"{name}: {info['description']}")
        for t in info["tools"]:
            print(f"  POST {t['endpoint']}  — {t['description']}")

    # Get tools for a specific server
    tools = discovery.list_tools("fetch")

    # Call a tool directly
    result = discovery.call_tool("fetch", "fetch", url="https://example.com")
    print(result)

Environment variables (set automatically in the terminal container):
    MCPO_URL      — mcpo gateway base URL  (default: http://mcpo:8000)
    REGISTRY_URL  — agentregistry base URL (default: http://agentregistry:8080)

Requires: httpx (pre-installed in the terminal sandbox)
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

__all__ = ["discover", "list_tools", "call_tool", "get_catalog"]

log = logging.getLogger("workbench.discovery")

MCPO_URL = os.environ.get("MCPO_URL", "http://mcpo:8000")
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://agentregistry:8080")

# Path to the bootstrap-generated static tool catalog (mounted in containers)
_TOOLS_JSON = Path(os.environ.get("WORKBENCH_TOOLS_JSON", "/opt/workbench/tools.json"))

_TIMEOUT = 10  # seconds for discovery HTTP calls
_TTL = 300     # cache lifetime in seconds


# ---------------------------------------------------------------------------
# Internal cache
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {}
_cache_ts: float = 0.0


def _cache_valid() -> bool:
    return bool(_cache) and (time.time() - _cache_ts) < _TTL


# ---------------------------------------------------------------------------
# Static catalog (bootstrap-generated)
# ---------------------------------------------------------------------------

def get_catalog() -> dict[str, Any]:
    """Return the bootstrap-generated tool catalog (no network calls).

    Falls back to empty dict if the file is missing.
    """
    try:
        return json.loads(_TOOLS_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Live discovery via mcpo
# ---------------------------------------------------------------------------

def list_tools(server: str) -> list[dict[str, Any]]:
    """Get available tools for an mcpo-proxied server via its OpenAPI spec.

    Args:
        server: mcpo server name (e.g. "fetch", "time")

    Returns:
        List of tool dicts with name, method, endpoint, description, and
        parameters (if available from the OpenAPI schema).
    """
    url = f"{MCPO_URL}/{server}/openapi.json"
    try:
        resp = httpx.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        spec = resp.json()
    except Exception as exc:
        log.warning("Failed to fetch OpenAPI for %s: %s", server, exc)
        return []

    tools: list[dict[str, Any]] = []
    schemas = spec.get("components", {}).get("schemas", {})

    for path, methods in spec.get("paths", {}).items():
        for method, detail in methods.items():
            tool_name = path.lstrip("/")
            tool_info: dict[str, Any] = {
                "name": tool_name,
                "method": method.upper(),
                "endpoint": f"{MCPO_URL}/{server}{path}",
                "description": detail.get("summary", detail.get("description", "")),
            }

            # Extract parameter schema if available
            body = detail.get("requestBody", {})
            content = body.get("content", {}).get("application/json", {})
            schema = content.get("schema", {})
            if "$ref" in schema:
                ref_name = schema["$ref"].rsplit("/", 1)[-1]
                schema = schemas.get(ref_name, schema)
            if schema.get("properties"):
                tool_info["parameters"] = {
                    k: {
                        "type": v.get("type", "string"),
                        "description": v.get("description", ""),
                        **({"required": True} if k in schema.get("required", []) else {}),
                    }
                    for k, v in schema["properties"].items()
                }

            tools.append(tool_info)

    return tools


def discover() -> dict[str, Any]:
    """Discover all mcpo-proxied tools with live OpenAPI details.

    Returns a dict keyed by mcpo server name::

        {
            "fetch": {
                "description": "URL fetch and content extraction",
                "tools": [{"name": "fetch", "endpoint": "...", ...}, ...]
            },
            ...
        }

    Results are cached with a TTL. Uses the static catalog as the baseline
    and enriches with live OpenAPI data from mcpo.
    """
    global _cache, _cache_ts

    if _cache_valid():
        return _cache

    catalog = get_catalog()
    mcpo_servers = catalog.get("mcpo_servers", {})

    result: dict[str, Any] = {}
    for name, info in mcpo_servers.items():
        tools = list_tools(name)
        result[name] = {
            "description": info.get("description", ""),
            "tools": tools,
            "docs_url": f"{MCPO_URL}/{name}/docs",
        }

    if result:
        _cache = result
        _cache_ts = time.time()
    elif _cache:
        log.warning("Live discovery failed; returning stale cache")
        return _cache

    return result


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def call_tool(server: str, tool: str, **kwargs: Any) -> Any:
    """Call an mcpo-proxied tool.

    Args:
        server: mcpo server name (e.g. "fetch")
        tool: tool name (e.g. "fetch")
        **kwargs: tool parameters

    Returns:
        Parsed JSON response from the tool.
    """
    url = f"{MCPO_URL}/{server}/{tool}"
    resp = httpx.post(url, json=kwargs, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Registry queries (optional — for full catalog browsing)
# ---------------------------------------------------------------------------

def registry_servers() -> list[dict[str, Any]]:
    """Query the agentregistry for all registered MCP servers."""
    try:
        resp = httpx.get(f"{REGISTRY_URL}/v0/servers", timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("servers", [])
    except Exception as exc:
        log.warning("Registry query failed: %s", exc)
        return []


def registry_agents() -> list[dict[str, Any]]:
    """Query the agentregistry for all registered agents."""
    try:
        resp = httpx.get(f"{REGISTRY_URL}/v0/agents", timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("agents", [])
    except Exception as exc:
        log.warning("Registry query failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    print("=== Static catalog ===")
    cat = get_catalog()
    if cat:
        pprint.pprint(cat)
    else:
        print("(no static catalog found)")

    print("\n=== Live mcpo discovery ===")
    tools = discover()
    if tools:
        for srv, info in tools.items():
            print(f"\n  {srv}: {info['description']}")
            for t in info["tools"]:
                params = t.get("parameters", {})
                param_str = ", ".join(
                    f"{k}{'*' if p.get('required') else ''}"
                    for k, p in params.items()
                ) if params else ""
                print(f"    POST {t['name']}({param_str})  — {t['description']}")
    else:
        print("(no mcpo tools discovered)")
