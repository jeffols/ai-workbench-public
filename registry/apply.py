#!/usr/bin/env python3
"""Apply or delete workbench registry artifacts via the Agentregistry API.

Usage:
    python3 registry/apply.py              # apply all artifacts
    python3 registry/apply.py --delete     # delete all artifacts
    python3 registry/apply.py --dry-run    # show what would be applied
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

import yaml  # type: ignore

REGISTRY_URL = "http://registry.localhost"
SCHEMA_URI = "https://static.modelcontextprotocol.io/schemas/2025-10-17/server.schema.json"
CATALOG = Path(__file__).resolve().parent / "catalog.yaml"

# Map declarative kind → API path and body builder
KIND_MAP = {
    "MCPServer": "servers",
    "Agent": "agents",
    "Skill": "skills",
    "Prompt": "prompts",
}


def _api(method: str, path: str, body: dict | None = None) -> dict | None:
    """Send an HTTP request to the registry API."""
    url = f"{REGISTRY_URL}/v0/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f"  ✗ {method} {path} → {e.code}: {err_body}", file=sys.stderr)
        return None


def build_server_body(meta: dict, spec: dict) -> dict:
    return {
        "$schema": SCHEMA_URI,
        "name": meta["name"],
        "description": spec["description"],
        "version": meta["version"],
        "packages": spec.get("packages", []),
    }


def build_agent_body(meta: dict, spec: dict) -> dict:
    body: dict = {
        "name": meta["name"],
        "version": meta["version"],
        "description": spec.get("description", ""),
    }
    for key in ("title", "language", "framework", "modelProvider", "modelName",
                "image", "mcpServers", "skills", "prompts", "status",
                "telemetryEndpoint", "repository"):
        if key in spec:
            body[key] = spec[key]
    return body


def build_skill_body(meta: dict, spec: dict) -> dict:
    body: dict = {
        "name": meta["name"],
        "version": meta["version"],
        "description": spec.get("description", ""),
    }
    for key in ("title", "category", "packages", "remotes", "repository",
                "status", "websiteUrl"):
        if key in spec:
            body[key] = spec[key]
    return body


def build_prompt_body(meta: dict, spec: dict) -> dict:
    return {
        "name": meta["name"],
        "version": meta["version"],
        "description": spec.get("description", ""),
        "content": spec.get("content", ""),
    }


BUILDERS = {
    "MCPServer": build_server_body,
    "Agent": build_agent_body,
    "Skill": build_skill_body,
    "Prompt": build_prompt_body,
}


def load_documents(path: Path) -> list[dict]:
    with open(path) as f:
        return [doc for doc in yaml.safe_load_all(f) if doc]


def apply(docs: list[dict], dry_run: bool = False) -> int:
    errors = 0
    for doc in docs:
        kind = doc["kind"]
        meta = doc["metadata"]
        spec = doc.get("spec", {})
        endpoint = KIND_MAP[kind]
        builder = BUILDERS[kind]
        body = builder(meta, spec)

        label = f"{kind} {meta['name']}@{meta['version']}"
        if dry_run:
            print(f"  → {label}")
            continue

        result = _api("POST", endpoint, body)
        if result:
            print(f"  ✓ {label}")
        else:
            errors += 1
    return errors


def delete(docs: list[dict], dry_run: bool = False) -> int:
    errors = 0
    for doc in reversed(docs):
        kind = doc["kind"]
        meta = doc["metadata"]
        endpoint = KIND_MAP[kind]
        name_encoded = meta["name"].replace("/", "%2F")
        path = f"{endpoint}/{name_encoded}/versions/{meta['version']}"

        label = f"{kind} {meta['name']}@{meta['version']}"
        if dry_run:
            print(f"  ✗ {label}")
            continue

        result = _api("DELETE", path)
        if result:
            print(f"  ✗ {label} deleted")
        else:
            errors += 1
    return errors


def main():
    parser = argparse.ArgumentParser(description="Apply workbench registry artifacts")
    parser.add_argument("--delete", action="store_true", help="delete all artifacts")
    parser.add_argument("--dry-run", action="store_true", help="show what would be applied")
    parser.add_argument("--file", type=Path, default=CATALOG, help="catalog YAML file")
    args = parser.parse_args()

    docs = load_documents(args.file)
    print(f"Loaded {len(docs)} artifacts from {args.file.name}")

    if args.delete:
        errors = delete(docs, args.dry_run)
    else:
        errors = apply(docs, args.dry_run)

    if errors:
        print(f"\n{errors} error(s)", file=sys.stderr)
        sys.exit(1)
    else:
        action = "would apply" if args.dry_run else ("deleted" if args.delete else "applied")
        print(f"\n{len(docs)} artifacts {action}")


if __name__ == "__main__":
    main()
