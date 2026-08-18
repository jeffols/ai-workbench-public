#!/usr/bin/env python3
"""Bootstrap the AI Workbench.

Generates:
  .env                          — from .env.example (secrets auto-generated)
  config/litellm/config.yaml    — LiteLLM model routing from .env
  config/caddy/Caddyfile        — slug-based MCP tool routes from mcp-tool.yaml manifests
  config/catalog/catalog.json   — tool catalog from mcp-tool.yaml manifests
  config/catalog/index.html     — human-readable catalog page

Also writes the TOOL_SERVER_CONNECTIONS value into .env so Open WebUI picks up
tool endpoints automatically via Caddy.
"""
from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml  # PyYAML
    def _load_yaml(path: Path) -> dict[str, Any]:
        return _yaml.safe_load(path.read_text()) or {}
except ImportError:
    # Inline YAML subset parser — handles the simple mcp-tool.yaml files
    # without requiring PyYAML as a host dependency.
    def _load_yaml(path: Path) -> dict[str, Any]:  # type: ignore[misc]
        import re
        data: dict[str, Any] = {}
        for line in path.read_text().splitlines():
            line = line.split("#")[0]  # strip comments
            if not line.strip() or line.startswith(" "):
                continue
            m = re.match(r"^(\w[\w_-]*):\s*(.*)", line)
            if m:
                key, val = m.group(1), m.group(2).strip()
                if val.startswith("[") and val.endswith("]"):
                    data[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
                elif val.startswith(">"):
                    data[key] = ""  # multiline — skip for now
                elif val in ("true", "false"):
                    data[key] = val == "true"
                elif val.isdigit():
                    data[key] = int(val)
                else:
                    data[key] = val.strip("'\"")
        return data


ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
ENV_FILE = ROOT / ".env"
SERVICES_DIR = ROOT / "services"
LITELLM_CONFIG = ROOT / "config" / "litellm" / "config.yaml"
CADDYFILE = ROOT / "config" / "caddy" / "Caddyfile"
CATALOG_DIR = ROOT / "config" / "catalog"
CATALOG_JSON = CATALOG_DIR / "catalog.json"
CATALOG_HTML = CATALOG_DIR / "index.html"
TOOL_CONNECTIONS_COMPOSE = ROOT / "config" / "openwebui" / "compose.tool-connections.yaml"
MCPO_CONFIG = ROOT / "config" / "mcpo" / "mcpo.json"
DISCOVERY_JSON = ROOT / "config" / "workbench-tools.json"
REGISTRY_CATALOG = ROOT / "registry" / "catalog.yaml"

# Keys whose values are auto-generated on first run.  --force preserves
# existing secrets; delete a key from .env to regenerate it.
_SECRET_KEYS = {"WEBUI_ADMIN_PASSWORD", "WEBUI_SECRET_KEY", "LITELLM_MASTER_KEY", "LITELLM_DB_PASSWORD", "OPEN_NOTEBOOK_ENCRYPTION_KEY", "OPEN_TERMINAL_API_KEY", "REGISTRY_DB_PASSWORD", "REGISTRY_JWT_KEY"}

# Keys that users fill in themselves — always preserve on --force.
_USER_KEYS = {
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_COPILOT_TOKEN",
    "AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION",
    "AZURE_FAST_MODEL", "AZURE_STRONG_MODEL", "AZURE_EMBEDDING_MODEL",
    "AZURE_GPT52_MODEL", "AZURE_GPT52_API_VERSION",
    "AZURE_GPT41MINI_MODEL",
    "CA_BUNDLE_PATH", "HTTPS_PROXY", "HTTP_PROXY",
    "OREILLY_AUTH_HEADER", "OREILLY_API_KEY",
    "CHAT_FRONTEND", "LOCAL_MODELS_ENDPOINT",
}

# Combined: keys that --force will never overwrite with .env.example values.
_PRESERVE_KEYS = _SECRET_KEYS | _USER_KEYS


# ---------------------------------------------------------------------------
# Tool manifest discovery
# ---------------------------------------------------------------------------

def discover_tools() -> list[dict[str, Any]]:
    """Find all mcp-tool.yaml manifests under services/."""
    tools: list[dict[str, Any]] = []
    if not SERVICES_DIR.is_dir():
        return tools
    for manifest in sorted(SERVICES_DIR.glob("*/mcp-tool.yaml")):
        tool = _load_yaml(manifest)
        if tool.get("slug"):
            tools.append(tool)
    return tools


# ---------------------------------------------------------------------------
# .env generation
# ---------------------------------------------------------------------------

def _generate(key: str) -> str:
    if key == "LITELLM_MASTER_KEY":
        return f"sk-{secrets.token_urlsafe(24)}"
    if key == "WEBUI_SECRET_KEY":
        return secrets.token_urlsafe(32)
    if key == "OPEN_NOTEBOOK_ENCRYPTION_KEY":
        return secrets.token_urlsafe(32)
    if key == "REGISTRY_JWT_KEY":
        return secrets.token_hex(32)  # 32-byte Ed25519 seed, hex-encoded
    return secrets.token_urlsafe(18)  # WEBUI_ADMIN_PASSWORD


def parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def render_env_if_needed(force: bool) -> None:
    if ENV_FILE.exists() and not force:
        return

    existing = parse_env(ENV_FILE) if ENV_FILE.exists() else {}

    lines: list[str] = []
    for raw_line in ENV_EXAMPLE.read_text().splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(raw_line)
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()

        if key in existing and existing[key] and key in _PRESERVE_KEYS:
            lines.append(f"{key}={existing[key]}")
        elif value == "__GENERATE__":
            lines.append(f"{key}={_generate(key)}")
        else:
            lines.append(raw_line)

    ENV_FILE.write_text("\n".join(lines) + "\n")
    print(f"wrote {ENV_FILE}")


def render_tool_connections_compose(tools: list[dict[str, Any]]) -> None:
    """Generate a Compose include that sets TOOL_SERVER_CONNECTIONS on Open WebUI."""
    connections = build_tool_connections(tools)
    blob = json.dumps(connections, indent=2)

    # Indent the JSON so it sits cleanly under the YAML key
    indented = "\n".join("        " + line for line in blob.splitlines())

    content = f"""\
# Generated by scripts/bootstrap.py — do not hand-edit.
# Sets TOOL_SERVER_CONNECTIONS for Open WebUI from mcp-tool.yaml manifests.

services:
  openwebui:
    environment:
      TOOL_SERVER_CONNECTIONS: >-
{indented}
"""
    TOOL_CONNECTIONS_COMPOSE.parent.mkdir(parents=True, exist_ok=True)
    TOOL_CONNECTIONS_COMPOSE.write_text(content)
    print(f"wrote {TOOL_CONNECTIONS_COMPOSE} ({len(connections)} tools)")


# ---------------------------------------------------------------------------
# LiteLLM config generation
# ---------------------------------------------------------------------------

def build_model_entry(
    alias: str,
    provider_model: str,
    api_key_env: str,
    *,
    api_base_env: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
    base_model: str | None = None,
    supports_max_completion_tokens: bool = False,
) -> str:
    lines = [
        f"\n  - model_name: {alias}",
        f"    litellm_params:",
        f"      model: {provider_model}",
        f"      api_key: os.environ/{api_key_env}",
    ]
    if api_base_env:
        lines.append(f"      api_base: os.environ/{api_base_env}")
    elif api_base:
        lines.append(f"      api_base: {api_base}")
    if api_version:
        lines.append(f'      api_version: "{api_version}"')
    model_info_lines = []
    if base_model:
        model_info_lines.append(f"      base_model: {base_model}")
    if supports_max_completion_tokens:
        model_info_lines.append(f"      supports_max_completion_tokens: true")
    if model_info_lines:
        lines.append(f"    model_info:")
        lines.extend(model_info_lines)
    return "\n".join(lines)


def build_local_model_entry(
    alias: str,
    model_id: str,
    api_base: str,
) -> str:
    """Build a LiteLLM model entry for a local OpenAI-compatible endpoint."""
    # Ensure api_base ends with /v1 for OpenAI-compatible endpoints
    if not api_base.endswith('/v1'):
        api_base = f"{api_base}/v1"
    
    lines = [
        f"\n  - model_name: {alias}",
        f"    litellm_params:",
        f"      model: openai/{model_id}",
        f"      api_base: {api_base}",
        f"      api_key: sk-local",  # placeholder key for local models
    ]
    return "\n".join(lines)


def render_litellm_config(env: dict[str, str]) -> None:
    sections: list[str] = ["model_list:"]
    has_embeddings = False

    openai_key = env.get("OPENAI_API_KEY", "")
    anthropic_key = env.get("ANTHROPIC_API_KEY", "")

    if openai_key:
        sections.append(
            build_model_entry(
                alias="openai-fast",
                provider_model=f"openai/{env.get('OPENAI_FAST_MODEL', 'gpt-4o-mini')}",
                api_key_env="OPENAI_API_KEY",
            )
        )
        sections.append("")
        sections.append(
            build_model_entry(
                alias="openai-strong",
                provider_model=f"openai/{env.get('OPENAI_STRONG_MODEL', 'gpt-4.1')}",
                api_key_env="OPENAI_API_KEY",
            )
        )
        sections.append("")

    if anthropic_key:
        sections.append(
            build_model_entry(
                alias="anthropic-fast",
                provider_model=f"anthropic/{env.get('ANTHROPIC_FAST_MODEL', 'claude-3-5-haiku-latest')}",
                api_key_env="ANTHROPIC_API_KEY",
            )
        )
        sections.append("")
        sections.append(
            build_model_entry(
                alias="anthropic-strong",
                provider_model=f"anthropic/{env.get('ANTHROPIC_STRONG_MODEL', 'claude-3-7-sonnet-latest')}",
                api_key_env="ANTHROPIC_API_KEY",
            )
        )
        sections.append("")

    github_token = env.get("GITHUB_COPILOT_TOKEN", "")
    if github_token:
        sections.append(
            build_model_entry(
                alias="claude-haiku-4.5",
                provider_model="github_copilot/claude-haiku-4.5",
                api_key_env="GITHUB_COPILOT_TOKEN",
            )
        )
        sections.append("")
        sections.append(
            build_model_entry(
                alias="claude-sonnet-4.6",
                provider_model="github_copilot/claude-sonnet-4.6",
                api_key_env="GITHUB_COPILOT_TOKEN",
            )
        )
        sections.append("")

    azure_key = env.get("AZURE_API_KEY", "")
    if azure_key:
        api_version = env.get("AZURE_API_VERSION", "2024-06-01")
        azure_fast = env.get("AZURE_FAST_MODEL", "")
        azure_strong = env.get("AZURE_STRONG_MODEL", "")
        if azure_fast:
            sections.append(
                build_model_entry(
                    alias="azure-fast",
                    provider_model=f"azure/{azure_fast}",
                    api_key_env="AZURE_API_KEY",
                    api_base_env="AZURE_API_BASE",
                    api_version=api_version,
                    base_model=f"azure/{azure_fast}",
                )
            )
            sections.append("")
        if azure_strong:
            sections.append(
                build_model_entry(
                    alias="azure-strong",
                    provider_model=f"azure/{azure_strong}",
                    api_key_env="AZURE_API_KEY",
                    api_base_env="AZURE_API_BASE",
                    api_version=api_version,
                    base_model=f"azure/{azure_strong}",
                )
            )
            sections.append("")
        azure_gpt41mini = env.get("AZURE_GPT41MINI_MODEL", "")
        if azure_gpt41mini:
            sections.append(
                build_model_entry(
                    alias="gpt-4.1-mini",
                    provider_model=f"azure/{azure_gpt41mini}",
                    api_key_env="AZURE_API_KEY",
                    api_base_env="AZURE_API_BASE",
                    api_version=api_version,
                    base_model=f"azure/{azure_gpt41mini}",
                )
            )
            sections.append("")
        azure_gpt52 = env.get("AZURE_GPT52_MODEL", "")
        if azure_gpt52:
            gpt52_version = env.get("AZURE_GPT52_API_VERSION", "2025-12-11")
            sections.append(
                build_model_entry(
                    alias="azure-gpt-5.2",
                    provider_model=f"azure/{azure_gpt52}",
                    api_key_env="AZURE_API_KEY",
                    api_base_env="AZURE_API_BASE",
                    api_version=gpt52_version,
                    base_model=f"azure/{azure_gpt52}",
                    supports_max_completion_tokens=True,
                )
            )
            sections.append("")
        azure_embedding = env.get("AZURE_EMBEDDING_MODEL", "")
        if azure_embedding:
            has_embeddings = True
            sections.append(
                build_model_entry(
                    alias="embeddings",
                    provider_model=f"azure/{azure_embedding}",
                    api_key_env="AZURE_API_KEY",
                    api_base_env="AZURE_API_BASE",
                    api_version=api_version,
                    base_model=f"azure/{azure_embedding}",
                )
            )
            sections.append("")

    # Fall back to OpenAI embeddings when Azure didn't provide one
    if not has_embeddings and openai_key:
        openai_embedding = env.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        if openai_embedding:
            sections.append(
                build_model_entry(
                    alias="embeddings",
                    provider_model=f"openai/{openai_embedding}",
                    api_key_env="OPENAI_API_KEY",
                )
            )
            sections.append("")

    # Local models from OpenAI-compatible endpoints (e.g., Foundry)
    local_endpoint = env.get("LOCAL_MODELS_ENDPOINT", "").strip()
    if local_endpoint:
        # Try to fetch available models from the local endpoint
        # Bootstrap runs on the host, so try 127.0.0.1 first for model discovery
        # Then use the configured endpoint (which may be IP for container access)
        fetch_urls = [
            "http://127.0.0.1:59601",  # Try localhost first (for bootstrap discovery)
        ]
        # Also try other variations
        if "127.0.0.1" not in local_endpoint and "localhost" not in local_endpoint:
            fetch_urls.append(local_endpoint)
        
        models_fetched = False
        for fetch_url in fetch_urls:
            if models_fetched:
                break
            try:
                import urllib.request
                import json
                req = urllib.request.Request(f"{fetch_url}/v1/models")
                with urllib.request.urlopen(req, timeout=5) as response:
                    models_data = json.loads(response.read().decode())
                    if "data" in models_data:
                        for model in models_data["data"]:
                            model_id = model.get("id", "")
                            if model_id:
                                # Create an alias like "local-phi-4-mini-instruct-generic-gpu"
                                # Sanitize the model ID for a valid alias
                                alias = f"local-{model_id.lower().replace(':', '-').replace('_', '-')}"
                                sections.append(
                                    build_local_model_entry(
                                        alias=alias,
                                        model_id=model_id,
                                        api_base=local_endpoint,  # use the configured endpoint for litellm
                                    )
                                )
                                sections.append("")
                                
                                # Add a short alias if the model contains a recognizable name (e.g., phi-4-mini)
                                # Extract the base name from patterns like "Phi-4-mini-instruct-generic-gpu:5"
                                lower_id = model_id.lower()
                                if "phi" in lower_id and "mini" in lower_id:
                                    short_alias = "phi-4-mini"
                                    sections.append(
                                        build_local_model_entry(
                                            alias=short_alias,
                                            model_id=model_id,
                                            api_base=local_endpoint,
                                        )
                                    )
                                    sections.append("")
                        models_fetched = True
            except Exception as e:
                pass  # try next URL
        
        if not models_fetched:
            print(f"warning: could not discover models from LOCAL_MODELS_ENDPOINT ({local_endpoint}); models may be in database")

    sections.append(
        """litellm_settings:
  callbacks: ["arize_phoenix"]

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  disable_spend_logs: true""")

    # ── Guardrails seam (S2 — opt-in, see docs/deployment-seams.md) ──────────
    # Only emitted when GUARDRAILS_ENABLED=true, so the lean default posture
    # (no guardrails profile running) generates the exact same config as before.
    if env.get("GUARDRAILS_ENABLED", "false").lower() == "true":
        guardrail_entries = []

        pii_entities = [
            e.strip() for e in env.get("PRESIDIO_PII_ENTITIES", "").split(",") if e.strip()
        ]
        pii_entities_yaml = "\n".join(
            f"        {entity}: \"MASK\"" for entity in pii_entities
        ) or "        {}"
        guardrail_entries.append(
            f"""  - guardrail_name: "presidio-dlp"
    litellm_params:
      guardrail: presidio
      mode: "pre_call"
      presidio_language: "en"
      default_on: true
      pii_entities_config:
{pii_entities_yaml}"""
        )

        if env.get("INJECTION_GUARDRAIL_ENABLED", "true").lower() == "true":
            guardrail_entries.append(
                """  - guardrail_name: "prompt-injection-heuristic"
    litellm_params:
      guardrail: guardrails.injection_guardrail.PromptInjectionGuardrail
      mode: "pre_call"
      default_on: true"""
            )

        sections.append("")
        sections.append("guardrails:")
        sections.append("\n".join(guardrail_entries))

    LITELLM_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    LITELLM_CONFIG.write_text("\n".join(sections).rstrip() + "\n")
    print(f"wrote {LITELLM_CONFIG}")


# ---------------------------------------------------------------------------
# Caddyfile generation
# ---------------------------------------------------------------------------

def render_caddyfile(tools: list[dict[str, Any]], env: dict[str, str] | None = None) -> None:
    """Generate a Caddyfile with slug-based reverse-proxy routes for MCP tools,
    subdomain routing for UI services, and health-proxy endpoints for the status page."""
    if env is None:
        env = {}

    # Health proxy routes reused across server blocks
    health_routes = [
        ("litellm",      "litellm:4000",       "/health"),
        ("litellm-models", "litellm:4000",     "/v1/models"),
        ("openwebui",    "openwebui:8080",      "/health"),
        ("phoenix",      "phoenix:6006",        "/healthz"),
        ("chroma",       "chroma:8000",         "/api/v2/heartbeat"),
        ("surrealdb",    "surrealdb:8000",      "/health"),
        ("open-notebook", "open-notebook:8502", "/healthz"),
        ("mcpo",         "mcpo:8000",           "/fetch/docs"),
        ("open-terminal", "open-terminal:8000", "/health"),
        ("agentregistry", "agentregistry:8080", "/v0/health"),
    ]
    # Onyx health routes (only when Onyx is the active frontend)
    chat_frontend = env.get("CHAT_FRONTEND", "openwebui")
    if chat_frontend == "onyx":
        health_routes.append(("onyx-api", "onyx-api-server:8080", "/health"))
        health_routes.append(("onyx-web", "onyx-web-server:3000", "/api/health"))
    for tool in tools:
        slug = tool["slug"]
        port = tool.get("port", 8000)
        hp = tool.get("health_path", "/healthz")
        health_routes.append((slug, f"{slug}:{port}", hp))

    def _health_block(indent: str = "    ") -> list[str]:
        """Return Caddy health-proxy handles."""
        block: list[str] = []
        for name, upstream, path in health_routes:
            needs_auth = upstream.startswith("litellm:")
            block.append(f"{indent}handle_path /api/health/{name} {{")
            block.append(f'{indent}    rewrite * {path}')
            if needs_auth:
                block.append(f"{indent}    reverse_proxy {upstream} {{")
                block.append(f'{indent}        header_up Authorization "Bearer {{env.LITELLM_MASTER_KEY}}"')
                block.append(f"{indent}    }}")
            else:
                block.append(f"{indent}    reverse_proxy {upstream}")
            block.append(f"{indent}}}")
        return block

    # ── :8090 block (internal MCP + API + catalog) ────────────────────────
    lines = [
        "# Generated by scripts/bootstrap.py — do not hand-edit.",
        "",
        ":8090 {",
    ]

    for tool in tools:
        slug = tool["slug"]
        port = tool.get("port", 8000)
        lines.append(f"    handle_path /mcp/{slug}/* {{")
        lines.append(f"        reverse_proxy {slug}:{port}")
        lines.append(f"    }}")
        lines.append("")

    lines.extend(_health_block())
    lines.append("")
    lines.append("    # Sandbox file downloads — serves output files from code-agent jobs")
    lines.append("    handle_path /sandbox/files/* {")
    lines.append("        rewrite * /files/read?path=/home/user/jobs{uri}")
    lines.append("        reverse_proxy open-terminal:8000 {")
    lines.append('            header_up Authorization "Bearer {env.OPEN_TERMINAL_API_KEY}"')
    lines.append("        }")
    lines.append("    }")
    lines.append("")
    lines.append("    # mcpo OpenAPI docs — browse MCP tool REST endpoints")
    lines.append("    handle_path /mcpo/* {")
    lines.append("        reverse_proxy mcpo:8000")
    lines.append("    }")
    lines.append("")
    lines.append("    handle_path /catalog* {")
    lines.append("        root * /srv/catalog")
    lines.append("        file_server")
    lines.append("    }")
    lines.append("")
    lines.append("    redir / /catalog/ permanent")
    lines.append("")
    lines.append("    handle /health {")
    lines.append('        respond "ok" 200')
    lines.append("    }")
    lines.append("}")

    # ── Subdomain blocks on :80 ───────────────────────────────────────────
    # Chat frontend seam — route chat.localhost based on CHAT_FRONTEND
    chat_frontend = env.get("CHAT_FRONTEND", "openwebui")
    if chat_frontend == "onyx":
        chat_upstream = "onyx-nginx:80"
    else:
        chat_upstream = "openwebui:8080"

    subdomains = [
        ("chat",     chat_upstream),
        ("litellm",  "litellm:4000"),
        ("phoenix",  "phoenix:6006"),
        ("notebook", "open-notebook:8502"),
        ("registry", "agentregistry:8080"),
    ]

    # Root domain — status page
    lines.append("")
    lines.append("http://ai-workbench.localhost, http://status.localhost {")
    lines.extend(_health_block())
    lines.append("")

    # MCP route probes — exercise the real /mcp/{slug}/* path that Open WebUI uses
    for tool in tools:
        slug = tool["slug"]
        port = tool.get("port", 8000)
        mcp_path = tool.get("mcp_path", "/mcp")
        lines.append(f"    handle_path /api/mcp-probe/{slug} {{")
        lines.append(f"        rewrite * {mcp_path}")
        lines.append(f"        reverse_proxy {slug}:{port}")
        lines.append(f"    }}")

    lines.append("")
    lines.append("    handle /health {")
    lines.append('        respond "ok" 200')
    lines.append("    }")
    lines.append("")
    lines.append("    handle_path /catalog* {")
    lines.append("        root * /srv/catalog")
    lines.append("        file_server")
    lines.append("    }")
    lines.append("")
    lines.append("    redir / /catalog/ permanent")
    lines.append("}")

    for sub, upstream in subdomains:
        lines.append("")
        lines.append(f"http://{sub}.localhost {{")
        lines.append(f"    reverse_proxy {upstream}")
        lines.append("}")

    # files.localhost — serves job output files from Open Terminal
    lines.append("")

    # terminal.localhost — full Open Terminal API for browser-side access
    lines.append("http://terminal.localhost {")
    lines.append("    reverse_proxy open-terminal:8000 {")
    lines.append('        header_up Authorization "Bearer {env.OPEN_TERMINAL_API_KEY}"')
    lines.append("    }")
    lines.append("}")
    lines.append("")
    lines.append("http://files.localhost {")
    lines.append("    # API passthrough for the browser UI")
    lines.append("    handle_path /_api/* {")
    lines.append("        reverse_proxy open-terminal:8000 {")
    lines.append('            header_up Authorization "Bearer {env.OPEN_TERMINAL_API_KEY}"')
    lines.append("        }")
    lines.append("    }")
    lines.append("    # Serve the HTML file browser for all other paths")
    lines.append("    handle {")
    lines.append("        root * /srv/files-browser")
    lines.append("        try_files /index.html")
    lines.append("        file_server")
    lines.append("    }")
    lines.append("}")

    lines.append("")

    CADDYFILE.parent.mkdir(parents=True, exist_ok=True)
    CADDYFILE.write_text("\n".join(lines))
    print(f"wrote {CADDYFILE} ({len(tools)} tool routes, {len(subdomains)} subdomains)")


# ---------------------------------------------------------------------------
# TOOL_SERVER_CONNECTIONS generation (for Open WebUI)
# ---------------------------------------------------------------------------

def build_tool_connections(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the Open WebUI TOOL_SERVER_CONNECTIONS JSON array from manifests."""
    connections: list[dict[str, Any]] = []
    for tool in tools:
        slug = tool["slug"]
        mcp_path = tool.get("mcp_path", "/mcp")
        auth_mode = tool.get("auth", {})
        if isinstance(auth_mode, dict):
            auth_mode = auth_mode.get("mode", "none")
        if not auth_mode:
            auth_mode = "none"

        entry: dict[str, Any] = {
            "info": {"id": slug, "name": tool.get("name", slug)},
            "url": f"http://caddy:8090/mcp/{slug}{mcp_path}",
            "path": "",
            "type": "mcp",
            "spec_type": "url",
            "auth_type": auth_mode if auth_mode != "none" else "none",
            "key": "",
            "config": {"enable": True},
        }
        connections.append(entry)

    return connections


# ---------------------------------------------------------------------------
# Catalog generation
# ---------------------------------------------------------------------------

def render_catalog(tools: list[dict[str, Any]]) -> None:
    """Generate catalog.json and a live status dashboard (index.html)."""
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)

    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tools": tools,
    }
    CATALOG_JSON.write_text(json.dumps(catalog, indent=2) + "\n")
    print(f"wrote {CATALOG_JSON} ({len(tools)} tools)")

    # Build tool rows for the MCP tools table
    tool_rows = ""
    for t in tools:
        slug = t["slug"]
        tool_rows += (
            f'      <tr id="tool-row-{slug}">'
            f'<td><span id="health-{slug}" class="dot"></span></td>'
            f'<td><span id="mcp-probe-{slug}" class="dot"></span></td>'
            f'<td><code>{slug}</code></td>'
            f'<td>{t.get("name", slug)}</td>'
            f'<td>{t.get("description", "")}</td>'
            f'<td><span id="latency-{slug}" class="latency"></span></td>'
            f'<td><span id="error-{slug}" class="error-detail"></span></td>'
            f'</tr>\n'
        )

    # Emit tool list as JS data so we don't rely on DOM ID scanning
    tool_js_entries = ", ".join(
        f'{{ slug: "{t["slug"]}" }}'
        for t in tools
    )
    tool_js = f"const MCP_TOOLS = [{tool_js_entries}];"

    html = _STATUS_PAGE_TEMPLATE.replace("{{TOOL_ROWS}}", tool_rows)
    html = html.replace("{{GENERATED}}", catalog["generated_at"])
    html = html.replace("{{TOOL_JS_DATA}}", tool_js)

    CATALOG_HTML.write_text(html)
    print(f"wrote {CATALOG_HTML}")


_STATUS_PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Workbench</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 2rem;
           color: #1a1a2e; background: #fafafa; }
    h1 { color: #16213e; margin-bottom: .25rem; }
    h2 { color: #16213e; margin-top: 2rem; border-bottom: 2px solid #e0e0e0; padding-bottom: .3rem; }
    .subtitle { color: #888; margin-bottom: 2rem; }
    a { color: #0f3460; }
    code { background: #eee; padding: .15rem .4rem; border-radius: 3px; font-size: .9em; }

    /* Health dot */
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
           background: #ccc; margin-right: 6px; vertical-align: middle; }
    .dot.ok    { background: #22c55e; }
    .dot.err   { background: #ef4444; }
    .dot.warn  { background: #f59e0b; }
    .dot.unknown { background: #ccc; }

    /* Topology */
    .topo { display: flex; gap: 1.5rem; align-items: flex-start; flex-wrap: wrap;
            padding: 1.5rem; background: #fff; border-radius: 8px; border: 1px solid #e0e0e0; }
    .topo-col { display: flex; flex-direction: column; gap: .5rem; }
    .topo-col h3 { margin: 0 0 .3rem; font-size: .85em; text-transform: uppercase;
                   color: #888; letter-spacing: .05em; }
    .topo-arrow { display: flex; align-items: center; color: #aaa; font-size: 1.5rem;
                  padding-top: 1.2rem; }
    .node { display: flex; align-items: center; padding: .45rem .75rem; background: #f5f5f5;
            border: 1px solid #ddd; border-radius: 6px; font-size: .9em; white-space: nowrap; }
    .node a { text-decoration: none; }

    /* Tables */
    table { border-collapse: collapse; width: 100%; margin-top: .5rem; background: #fff;
            border-radius: 8px; overflow: hidden; border: 1px solid #e0e0e0; }
    th, td { text-align: left; padding: .5rem .75rem; border-bottom: 1px solid #eee; }
    th { background: #16213e; color: #fff; font-size: .85em; }
    tr:last-child td { border-bottom: none; }

    /* Model cards */
    .model-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
                  gap: .75rem; margin-top: .5rem; }
    .model-card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
                  padding: .75rem 1rem; }
    .model-card .alias { font-weight: 600; }
    .model-card .provider { color: #888; font-size: .85em; }
    .model-card .error { color: #ef4444; font-size: .8em; margin-top: .25rem;
                         max-height: 3em; overflow: hidden; }

    /* Setup */
    .setup { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
             padding: 1rem 1.25rem; margin-top: .5rem; }
    .setup pre { margin: .5rem 0 0; background: #1a1a2e; color: #e0e0e0; padding: .75rem 1rem;
                 border-radius: 6px; overflow-x: auto; font-size: .85em; }
    .meta { color: #aaa; font-size: .8em; margin-top: 2rem; }

    /* MCP connection chain */
    .chain { display: flex; align-items: center; gap: .75rem; padding: 1rem 1.25rem;
             background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
             margin-bottom: .75rem; flex-wrap: wrap; }
    .chain-hop { display: flex; align-items: center; padding: .4rem .75rem;
                 background: #f5f5f5; border: 1px solid #ddd; border-radius: 6px;
                 font-size: .9em; white-space: nowrap; }
    .chain-arrow { color: #aaa; font-size: 1.2rem; }
    .chain-label { font-size: .75em; color: #888; display: block; }

    /* Latency and error detail */
    .latency { font-size: .8em; color: #888; font-variant-numeric: tabular-nums; }
    .latency.slow { color: #f59e0b; }
    .error-detail { font-size: .8em; color: #ef4444; }
  </style>
</head>
<body>
  <h1>🔧 AI Workbench</h1>
  <p class="subtitle">Live system status &mdash; auto-refreshes every 30 s</p>

  <!-- ── Topology ─────────────────────────────────────────────── -->
  <h2>System Topology</h2>
  <div class="topo">
    <div class="topo-col">
      <h3>Providers</h3>
      <div class="node" id="node-openai"><span class="dot" id="health-provider-openai"></span> OpenAI</div>
      <div class="node" id="node-azure"><span class="dot" id="health-provider-azure"></span> Azure OpenAI</div>
      <div class="node" id="node-anthropic"><span class="dot" id="health-provider-anthropic"></span> Anthropic</div>
    </div>
    <div class="topo-arrow">→</div>
    <div class="topo-col">
      <h3>Gateway</h3>
      <div class="node"><span class="dot" id="health-litellm"></span>
        <a href="http://litellm.localhost">LiteLLM</a></div>
    </div>
    <div class="topo-arrow">→</div>
    <div class="topo-col">
      <h3>Frontends</h3>
      <div class="node"><span class="dot" id="health-openwebui"></span>
        <a href="http://chat.localhost">Open WebUI</a></div>
      <div class="node"><span class="dot" id="health-notebook"></span>
        <a href="http://notebook.localhost">Open Notebook</a></div>
    </div>
    <div class="topo-arrow" style="padding-left:1rem">‹</div>
    <div class="topo-col">
      <h3>Data</h3>
      <div class="node"><span class="dot" id="health-chroma"></span> Chroma</div>
      <div class="node"><span class="dot" id="health-surrealdb"></span> SurrealDB</div>
    </div>
  </div>
  <div class="topo" style="margin-top:.75rem;">
    <div class="topo-col">
      <h3>Compute</h3>
      <div class="node"><span class="dot" id="health-mcpo"></span> mcpo
        <span style="font-size:.75em;color:#888;">(tool gateway)</span></div>
      <div class="node"><span class="dot" id="health-open-terminal"></span> Open Terminal</div>
      <div class="node"><span class="dot" id="health-agentregistry"></span>
        <a href="http://registry.localhost">Agentregistry</a></div>
    </div>
    <div class="topo-arrow">→</div>
    <div class="topo-col">
      <h3>Observability</h3>
      <div class="node"><span class="dot" id="health-phoenix"></span>
        <a href="http://phoenix.localhost">Phoenix</a></div>
    </div>
    <div class="topo-arrow">‹ traces</div>
    <div class="topo-col" style="padding-top:1.2rem;">
      <div style="color:#888;font-size:.85em;">all services via OTEL</div>
    </div>
  </div>

  <!-- ── Model Health ─────────────────────────────────────────── -->
  <h2>Model Health <small style="font-weight:normal;color:#888;">(from LiteLLM /health)</small></h2>
  <div id="model-grid" class="model-grid">
    <div style="color:#888;">Loading…</div>
  </div>

  <!-- ── MCP Tools ────────────────────────────────────────────── -->
  <h2>MCP Tools</h2>

  <div class="chain">
    <div class="chain-hop">
      <span class="dot" id="health-chain-openwebui"></span>
      <span>Open WebUI<span class="chain-label">chat frontend</span></span>
    </div>
    <div class="chain-arrow">→</div>
    <div class="chain-hop">
      <span class="dot" id="health-chain-caddy"></span>
      <span>Caddy<span class="chain-label">reverse proxy :8090</span></span>
    </div>
    <div class="chain-arrow">→</div>
    <div class="chain-hop">
      <span class="dot unknown" id="health-chain-tools"></span>
      <span>MCP Services<span class="chain-label">per-tool status below ↓</span></span>
    </div>
  </div>

  <table>
    <thead><tr>
      <th title="Health endpoint through Caddy proxy">Health</th>
      <th title="MCP protocol route (same path Open WebUI uses)">MCP Route</th>
      <th>Slug</th>
      <th>Name</th>
      <th>Description</th>
      <th>Latency</th>
      <th>Error</th>
    </tr></thead>
    <tbody>
{{TOOL_ROWS}}    </tbody>
  </table>

  <!-- ── Setup ────────────────────────────────────────────────── -->
  <h2>Subdomain Setup</h2>
  <div class="setup">
    <p><code>.localhost</code> domains resolve to <code>127.0.0.1</code> automatically (RFC 6761) &mdash; no <code>/etc/hosts</code> needed.</p>
    <p style="margin-top:.75rem;font-size:.9em;">Visit
      <a href="http://status.localhost">status.localhost</a> or <a href="http://ai-workbench.localhost">ai-workbench.localhost</a> for this page,
      <a href="http://chat.localhost">chat.localhost</a> for Open WebUI,
      <a href="http://litellm.localhost">litellm.localhost</a>,
      <a href="http://phoenix.localhost">phoenix.localhost</a>,
      <a href="http://notebook.localhost">notebook.localhost</a>,
      <a href="http://files.localhost">files.localhost</a> for job output downloads.</p>
  </div>

  <p class="meta">Generated {{GENERATED}} by <code>scripts/bootstrap.py</code></p>

<script>
// ── Tool metadata (generated, not derived from DOM) ───────────
{{TOOL_JS_DATA}}

// ── Platform services ─────────────────────────────────────────
const PLATFORM_SERVICES = [
  { id: "litellm",        endpoint: "/api/health/litellm" },
  { id: "openwebui",      endpoint: "/api/health/openwebui" },
  { id: "phoenix",        endpoint: "/api/health/phoenix" },
  { id: "chroma",         endpoint: "/api/health/chroma" },
  { id: "surrealdb",      endpoint: "/api/health/surrealdb" },
  { id: "mcpo",           endpoint: "/api/health/mcpo" },
  { id: "open-terminal",  endpoint: "/api/health/open-terminal" },
  { id: "agentregistry",  endpoint: "/api/health/agentregistry" },
  { id: "notebook",       endpoint: "/api/health/open-notebook" },
];

function setDot(id, cls) {
  const el = document.getElementById(id);
  if (el) { el.className = "dot " + cls; }
}

function setText(id, text, cls) {
  const el = document.getElementById(id);
  if (el) { el.textContent = text; el.className = cls || ""; }
}

// ── Platform health (parallel) ────────────────────────────────
async function checkPlatform() {
  await Promise.allSettled(PLATFORM_SERVICES.map(async (svc) => {
    try {
      const r = await fetch(svc.endpoint, { signal: AbortSignal.timeout(5000) });
      setDot("health-" + svc.id, r.ok ? "ok" : "err");
    } catch {
      setDot("health-" + svc.id, "err");
    }
  }));
}

// ── MCP connection chain (Caddy first, then per-tool) ─────────
let caddyOk = false;

async function checkMcpChain() {
  // Hop 1: Open WebUI
  try {
    const r = await fetch("/api/health/openwebui", { signal: AbortSignal.timeout(5000) });
    setDot("health-chain-openwebui", r.ok ? "ok" : "err");
  } catch {
    setDot("health-chain-openwebui", "err");
  }

  // Hop 2: Caddy self-health
  caddyOk = false;
  try {
    const r = await fetch("/health", { signal: AbortSignal.timeout(3000) });
    caddyOk = r.ok;
    setDot("health-chain-caddy", caddyOk ? "ok" : "err");
  } catch {
    setDot("health-chain-caddy", "err");
  }

  if (!caddyOk) {
    // Caddy is down — mark all tools as unknown (gray)
    let anyOk = false;
    for (const tool of MCP_TOOLS) {
      setDot("health-" + tool.slug, "unknown");
      setDot("mcp-probe-" + tool.slug, "unknown");
      setText("latency-" + tool.slug, "—", "latency");
      setText("error-" + tool.slug, "caddy unreachable", "error-detail");
    }
    setDot("health-chain-tools", "unknown");
    return;
  }

  // Hop 3: Per-tool checks (health endpoint + MCP route probe, in parallel)
  let allOk = true;
  let anyOk = false;

  await Promise.allSettled(MCP_TOOLS.map(async (tool) => {
    // Check 1: health endpoint (through Caddy)
    try {
      const r = await fetch(`/api/health/${tool.slug}`, { signal: AbortSignal.timeout(5000) });
      setDot("health-" + tool.slug, r.ok ? "ok" : "err");
      if (!r.ok) allOk = false; else anyOk = true;
    } catch {
      setDot("health-" + tool.slug, "err");
      allOk = false;
    }

    // Check 2: MCP route probe (exercises the exact /mcp/{slug}/* path Open WebUI uses)
    const t0 = performance.now();
    try {
      const r = await fetch(`/api/mcp-probe/${tool.slug}`, { signal: AbortSignal.timeout(5000) });
      const ms = Math.round(performance.now() - t0);
      setDot("mcp-probe-" + tool.slug, r.ok ? "ok" : "err");
      setText("latency-" + tool.slug, ms + "ms", "latency" + (ms > 2000 ? " slow" : ""));
      if (r.ok) {
        setText("error-" + tool.slug, "", "error-detail");
      } else {
        setText("error-" + tool.slug, `HTTP ${r.status}`, "error-detail");
        allOk = false;
      }
    } catch (e) {
      const ms = Math.round(performance.now() - t0);
      setDot("mcp-probe-" + tool.slug, "err");
      const reason = ms >= 4900 ? "timeout" : "network error";
      setText("latency-" + tool.slug, ms >= 4900 ? ">5s" : ms + "ms", "latency slow");
      setText("error-" + tool.slug, reason, "error-detail");
      allOk = false;
    }
  }));

  setDot("health-chain-tools", allOk ? "ok" : (anyOk ? "warn" : "err"));
}

// ── LiteLLM model-level health ────────────────────────────────
async function checkModels() {
  const grid = document.getElementById("model-grid");
  try {
    const r = await fetch("/api/health/litellm", { signal: AbortSignal.timeout(8000) });
    if (!r.ok) { grid.innerHTML = '<div style="color:#ef4444">LiteLLM unreachable</div>'; return; }
    const data = await r.json();
    const healthy   = (data.healthy_endpoints   || []).map(e => ({ ...e, _ok: true }));
    const unhealthy = (data.unhealthy_endpoints || []).map(e => ({ ...e, _ok: false }));
    const all = [...healthy, ...unhealthy];

    // Determine provider health for topology
    const providers = { openai: null, azure: null, anthropic: null };
    for (const m of all) {
      const model = m.model || "";
      let p = null;
      if (model.startsWith("azure/"))     p = "azure";
      else if (model.startsWith("openai/") || model.startsWith("text-embedding")) p = "openai";
      else if (model.startsWith("anthropic/")) p = "anthropic";
      if (p) {
        if (providers[p] === null) providers[p] = m._ok;
        else providers[p] = providers[p] || m._ok; // any healthy = provider ok
      }
    }
    for (const [p, ok] of Object.entries(providers)) {
      setDot("health-provider-" + p, ok === null ? "" : (ok ? "ok" : "err"));
    }

    if (all.length === 0) { grid.innerHTML = '<div style="color:#888">No models configured</div>'; return; }

    grid.innerHTML = all.map(m => {
      const cls = m._ok ? "ok" : "err";
      const alias = m.model || "?";
      const errHtml = m._ok ? "" : `<div class="error">${esc(extractError(m))}</div>`;
      return `<div class="model-card"><span class="dot ${cls}"></span>` +
             `<span class="alias">${esc(alias)}</span>${errHtml}</div>`;
    }).join("");
  } catch {
    grid.innerHTML = '<div style="color:#ef4444">Could not reach LiteLLM</div>';
  }
}

function extractError(m) {
  const e = m.error || "";
  // Strip HTML noise from proxy errors
  const match = e.match(/APIError.*?- (.+)/s);
  return match ? match[1].replace(/<[^>]+>/g, "").trim().slice(0, 120) : String(e).slice(0, 120);
}

function esc(s) {
  const d = document.createElement("div"); d.textContent = s; return d.innerHTML;
}

// ── Init ──────────────────────────────────────────────────────
async function refresh() {
  await Promise.all([checkPlatform(), checkMcpChain(), checkModels()]);
}
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Discovery JSON generation (for agents and terminal)
# ---------------------------------------------------------------------------

def _load_catalog_descriptions() -> dict[str, str]:
    """Extract server descriptions from registry/catalog.yaml.

    Returns a mapping from short name (e.g. "fetch") to description.
    """
    descs: dict[str, str] = {}
    if not REGISTRY_CATALOG.is_file():
        return descs

    try:
        import yaml as _y
        docs = list(_y.safe_load_all(REGISTRY_CATALOG.read_text()))
    except ImportError:
        return descs  # no PyYAML, skip adding descriptions
    except Exception:
        return descs

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind", "")
        meta = doc.get("metadata", {})
        spec = doc.get("spec", {})
        if kind == "MCPServer":
            name = meta.get("name", "")
            # workbench/magic-fetch → magic-fetch
            short = name.replace("workbench/", "").replace("-mcp", "")
            desc = spec.get("description", "")
            if short and desc:
                descs[short] = desc

    return descs


def render_discovery_json(tools: list[dict[str, Any]]) -> None:
    """Generate config/workbench-tools.json for agent and terminal discovery.

    Reads the mcpo config to identify proxied servers, enriches with
    descriptions from the registry catalog and mcp-tool.yaml manifests.
    """
    mcpo_servers: dict[str, Any] = {}

    # Read mcpo config for server names
    if MCPO_CONFIG.is_file():
        try:
            mcpo_cfg = json.loads(MCPO_CONFIG.read_text())
            for name in mcpo_cfg.get("mcpServers", {}):
                mcpo_servers[name] = {
                    "description": "",
                    "docs_url": f"http://mcpo:8000/{name}/docs",
                    "openapi_url": f"http://mcpo:8000/{name}/openapi.json",
                }
        except (json.JSONDecodeError, KeyError):
            pass

    # Enrich with catalog descriptions
    catalog_descs = _load_catalog_descriptions()
    for name in mcpo_servers:
        if name in catalog_descs:
            mcpo_servers[name]["description"] = catalog_descs[name]

    # Fallback: enrich from mcp-tool.yaml manifests
    tool_by_slug = {t["slug"]: t for t in tools}
    for name in mcpo_servers:
        if not mcpo_servers[name]["description"]:
            slug = f"{name}-mcp"
            tool = tool_by_slug.get(slug, {})
            mcpo_servers[name]["description"] = tool.get("description", "")

    discovery = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mcpo_url": "http://mcpo:8000",
        "registry_url": "http://agentregistry:8080",
        "mcpo_servers": mcpo_servers,
    }

    DISCOVERY_JSON.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY_JSON.write_text(json.dumps(discovery, indent=2) + "\n")
    print(f"wrote {DISCOVERY_JSON} ({len(mcpo_servers)} mcpo servers)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the AI Workbench")
    parser.add_argument("--force", action="store_true", help="re-render .env and all generated config")
    args = parser.parse_args()

    render_env_if_needed(force=args.force)
    env = parse_env(ENV_FILE)

    tools = discover_tools()
    print(f"discovered {len(tools)} MCP tool(s): {[t['slug'] for t in tools]}")

    render_litellm_config(env)
    render_caddyfile(tools, env)
    render_catalog(tools)

    # Tool connections are only needed for Open WebUI; Onyx manages its own
    chat_frontend = env.get("CHAT_FRONTEND", "openwebui")
    if chat_frontend == "openwebui":
        render_tool_connections_compose(tools)
    else:
        print(f"CHAT_FRONTEND={chat_frontend} — skipping Open WebUI tool connections")

    render_discovery_json(tools)


if __name__ == "__main__":
    main()
