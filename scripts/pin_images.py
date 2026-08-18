#!/usr/bin/env python3
"""Pin Docker images in .env to the digests currently running.

Reads each `*_IMAGE=<repo>:<tag>` line in .env, resolves the currently
running container's image digest via `docker inspect`, and rewrites the
.env line to `<repo>@sha256:<digest>`.  .env.example is not touched; it
stays at human-readable tags.

Usage:
    make up
    make pin-images      # captures the digests of what just started
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

# Map IMAGE env var → container name.  Keep in sync with docker-compose.yml
# + services/*/compose.mcp.yaml.  Container name defaults to the Compose
# service name under project `ai-workbench` (so `ai-workbench-<svc>-1`).
IMAGE_VAR_TO_SERVICE = {
    "CADDY_IMAGE": "caddy",
    "PHOENIX_IMAGE": "phoenix",
    "LITELLM_DB_IMAGE": "litellm-db",
    "LITELLM_IMAGE": "litellm",
    "CHROMA_IMAGE": "chroma",
    "SURREALDB_IMAGE": "surrealdb",
    "OPEN_NOTEBOOK_IMAGE": "open-notebook",
    "OPENWEBUI_IMAGE": "openwebui",
    "MCPO_IMAGE": "mcpo",
    "OPEN_TERMINAL_IMAGE": "open-terminal",
    "AGENTREGISTRY_IMAGE": "agentregistry",
}

PROJECT = "ai-workbench"


def _docker_inspect_image_id(container: str) -> str | None:
    """Return the RepoDigest for the image of a running container, or None."""
    try:
        raw = subprocess.check_output(
            ["docker", "inspect", container], stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return None
    data = json.loads(raw)
    if not data:
        return None
    image_id = data[0].get("Image", "")
    if not image_id:
        return None

    # Resolve the image's RepoDigest (most reliable: matches registry digest).
    try:
        img_raw = subprocess.check_output(
            ["docker", "image", "inspect", image_id], stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return None
    img_data = json.loads(img_raw)
    if not img_data:
        return None
    repo_digests = img_data[0].get("RepoDigests") or []
    # Prefer a digest that matches the repo name we're pinning (first match).
    return repo_digests[0] if repo_digests else None


def _container_name(service: str) -> str:
    return f"{PROJECT}-{service}-1"


def main() -> int:
    if not ENV_FILE.exists():
        print(f"error: {ENV_FILE} not found — run `make bootstrap` first",
              file=sys.stderr)
        return 1

    lines = ENV_FILE.read_text().splitlines()
    changed = 0
    missing: list[str] = []

    for i, line in enumerate(lines):
        m = re.match(r"^([A-Z_]+_IMAGE)=(.+)$", line.strip())
        if not m:
            continue
        var, _current = m.group(1), m.group(2)
        service = IMAGE_VAR_TO_SERVICE.get(var)
        if not service:
            continue

        digest = _docker_inspect_image_id(_container_name(service))
        if not digest:
            missing.append(f"{var} (container {_container_name(service)} not running or no RepoDigest)")
            continue

        new_line = f"{var}={digest}"
        if new_line != line:
            lines[i] = new_line
            changed += 1
            print(f"pinned {var} → {digest}")

    if changed:
        ENV_FILE.write_text("\n".join(lines) + "\n")
        print(f"\nwrote {changed} pin(s) to {ENV_FILE}")
    else:
        print("no changes")

    if missing:
        print("\nskipped (not running or no digest):", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        # Not a hard error: some services (agentregistry locally built, or
        # notebook profile not started) legitimately won't be present.
        return 0 if changed else 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
