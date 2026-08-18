#!/usr/bin/env python3
"""Redact secrets from resolved Docker Compose YAML before sharing.

Usage:
    docker compose --profile tools config | python3 scripts/sanitize_compose.py > /tmp/sanitized.yaml
    # or
    python3 scripts/sanitize_compose.py resolved.yaml > /tmp/sanitized.yaml
"""

import re
import sys
from pathlib import Path

import yaml

REDACTED = "REDACTED"

# Substrings that flag an env-var key or YAML key as sensitive
SENSITIVE_PATTERNS = re.compile(
    r"(password|secret|token|api_key|apikey|api\.key|master_key|"
    r"access_key|private_key|encryption_key|jwt_key|ca_bundle|"
    r"database_url|db_url|connection_string)",
    re.IGNORECASE,
)

# Keys that match SENSITIVE_PATTERNS but are actually safe config flags
FALSE_POSITIVES = re.compile(
    r"^(ENABLE_PASSWORD_AUTH|ENABLE_PERSISTENT_CONFIG|SURREAL_PASSWORD)$",
    re.IGNORECASE,
)


def is_sensitive(key: str) -> bool:
    return bool(SENSITIVE_PATTERNS.search(key)) and not bool(FALSE_POSITIVES.match(key))


def redact_env_dict(env: dict) -> dict:
    return {k: (REDACTED if is_sensitive(k) else v) for k, v in env.items()}


def redact_env_list(env: list) -> list:
    out = []
    for item in env:
        if isinstance(item, str) and "=" in item:
            k, _, v = item.partition("=")
            out.append(f"{k}={REDACTED}" if is_sensitive(k) else item)
        else:
            out.append(item)
    return out


def redact_strings_deep(obj):
    """Walk any nested structure and redact string values that contain secrets."""
    if isinstance(obj, dict):
        return {k: redact_strings_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_strings_deep(v) for v in obj]
    if isinstance(obj, str) and is_sensitive(obj):
        return REDACTED
    return obj


def redact_services(cfg: dict) -> dict:
    for _name, svc in cfg.get("services", {}).items():
        env = svc.get("environment")
        if isinstance(env, dict):
            svc["environment"] = redact_env_dict(env)
        elif isinstance(env, list):
            svc["environment"] = redact_env_list(env)

        # Walk healthcheck, command, labels for embedded secrets
        for field in ("healthcheck", "command", "labels"):
            if field in svc:
                svc[field] = redact_strings_deep(svc[field])
    return cfg


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        text = Path(sys.argv[1]).read_text()
    else:
        text = sys.stdin.read()

    cfg = yaml.safe_load(text)
    cfg = redact_services(cfg)
    yaml.safe_dump(cfg, sys.stdout, default_flow_style=False, sort_keys=False)


if __name__ == "__main__":
    main()
