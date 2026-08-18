#!/usr/bin/env python3
"""Pre-flight validation of .env for the AI Workbench."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
SERVICES_DIR = ROOT / "services"

# Variables required for core services (always-on).
_CORE_REQUIRED = [
    "CADDY_IMAGE", "CADDY_PORT",
    "PHOENIX_IMAGE", "PHOENIX_PROJECT_NAME",
    "PHOENIX_ALLOW_EXTERNAL_RESOURCES", "PHOENIX_DEFAULT_RETENTION_POLICY_DAYS",
    "LITELLM_DB_IMAGE", "LITELLM_DB_PORT",
    "LITELLM_IMAGE", "LITELLM_MASTER_KEY", "LITELLM_DB_PASSWORD",
    "CHROMA_IMAGE",
    "OPENWEBUI_IMAGE", "OPENWEBUI_PORT",
    "WEBUI_ADMIN_EMAIL", "WEBUI_ADMIN_PASSWORD", "WEBUI_SECRET_KEY",
    "DEFAULT_CHAT_MODEL", "RAG_EMBEDDING_MODEL",
    "WORKBENCH_NO_PROXY",
]

# Additional variables required when the "notebook" profile is active.
_NOTEBOOK_REQUIRED = [
    "SURREALDB_IMAGE",
    "OPEN_NOTEBOOK_IMAGE", "OPEN_NOTEBOOK_PORT",
    "OPEN_NOTEBOOK_ENCRYPTION_KEY",
]

# Additional variables required when the "tools" profile is active.
_TOOLS_REQUIRED = [
    "AGENT_MODEL",
]

# Additional variables required when GUARDRAILS_ENABLED=true (guardrails profile).
_GUARDRAILS_REQUIRED = [
    "PRESIDIO_ANALYZER_IMAGE", "PRESIDIO_ANONYMIZER_IMAGE",
    "PRESIDIO_ANALYZER_API_BASE", "PRESIDIO_ANONYMIZER_API_BASE",
]

_PORT_KEYS = {"CADDY_PORT", "LITELLM_DB_PORT", "OPEN_NOTEBOOK_PORT", "OPENWEBUI_PORT"}


def parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _discover_slugs() -> list[str]:
    """Read slug from each mcp-tool.yaml manifest."""
    import re
    slugs: list[str] = []
    if not SERVICES_DIR.is_dir():
        return slugs
    for manifest in sorted(SERVICES_DIR.glob("*/mcp-tool.yaml")):
        for line in manifest.read_text().splitlines():
            m = re.match(r"^slug:\s*(.+)", line)
            if m:
                slugs.append(m.group(1).strip())
                break
    return slugs


def main() -> None:
    if not ENV_FILE.exists():
        print("missing .env; run python3 scripts/bootstrap.py first")
        sys.exit(1)

    env = parse_env(ENV_FILE)
    errors: list[str] = []

    # --- required-key checks (core + profile-aware) ---
    required = list(_CORE_REQUIRED)
    # Always check notebook + tools vars since bootstrap populates them all
    required.extend(_NOTEBOOK_REQUIRED)
    required.extend(_TOOLS_REQUIRED)
    if env.get("GUARDRAILS_ENABLED", "false").lower() == "true":
        required.extend(_GUARDRAILS_REQUIRED)

    missing = [key for key in required if not env.get(key)]
    if missing:
        errors.append(f"missing required values: {', '.join(missing)}")

    # --- port shape checks ---
    for key in _PORT_KEYS:
        val = env.get(key, "")
        if val and not val.isdigit():
            errors.append(f"{key}={val!r} is not a valid port number")

    # --- provider key logic ---
    if not env.get("OPENAI_API_KEY"):
        azure_can_embed = (
            env.get("AZURE_API_KEY")
            and env.get("AZURE_API_BASE")
            and env.get("AZURE_EMBEDDING_MODEL")
        )
        if not azure_can_embed:
            errors.append(
                "OPENAI_API_KEY is required for embeddings unless AZURE_API_KEY, "
                "AZURE_API_BASE, and AZURE_EMBEDDING_MODEL are all set"
            )

    default_model = env.get("DEFAULT_CHAT_MODEL", "")
    if default_model.startswith("anthropic") and not env.get("ANTHROPIC_API_KEY"):
        errors.append("DEFAULT_CHAT_MODEL points at Anthropic but ANTHROPIC_API_KEY is empty")
    if default_model.startswith("openai") and not env.get("OPENAI_API_KEY"):
        errors.append("DEFAULT_CHAT_MODEL points at OpenAI but OPENAI_API_KEY is empty")
    if default_model.startswith("azure"):
        if not env.get("AZURE_API_KEY"):
            errors.append("DEFAULT_CHAT_MODEL points at Azure but AZURE_API_KEY is empty")
        if not env.get("AZURE_API_BASE"):
            errors.append("DEFAULT_CHAT_MODEL points at Azure but AZURE_API_BASE is empty")

    # --- CA bundle path check ---
    ca_path = env.get("CA_BUNDLE_PATH", "")
    if ca_path and not Path(ca_path).exists():
        errors.append(f"CA_BUNDLE_PATH={ca_path!r} does not exist on host")

    # --- guardrails checks ---
    if env.get("GUARDRAILS_ENABLED", "false").lower() == "true":
        for key in ("PRESIDIO_ANALYZER_API_BASE", "PRESIDIO_ANONYMIZER_API_BASE"):
            val = env.get(key, "")
            if val and not (val.startswith("http://") or val.startswith("https://")):
                errors.append(f"{key}={val!r} must be a full http(s) URL")
        no_proxy = env.get("WORKBENCH_NO_PROXY", "")
        for host in ("presidio-analyzer", "presidio-anonymizer"):
            if host not in no_proxy:
                errors.append(
                    f"GUARDRAILS_ENABLED=true but '{host}' is missing from WORKBENCH_NO_PROXY"
                )

    # --- slug uniqueness ---
    slugs = _discover_slugs()
    seen: set[str] = set()
    for slug in slugs:
        if slug in seen:
            errors.append(f"duplicate MCP tool slug: {slug}")
        seen.add(slug)

    if errors:
        print("validation failed:\n")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)

    print(f"env looks good ({len(slugs)} MCP tool(s) discovered)")


if __name__ == "__main__":
    main()
