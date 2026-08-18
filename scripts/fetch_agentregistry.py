#!/usr/bin/env python3
"""Shallow-clone the agentregistry upstream repo and stage the workbench
slim Dockerfile inside it, ready for ``docker compose build agentregistry``.

The Compose fragment builds from ``services/agentregistry/upstream/`` using
``server.Dockerfile`` (copied here from ``services/agentregistry/``).  The
upstream directory is gitignored — it's a build artifact, not source.

Delete ``services/agentregistry/upstream/`` to force a re-clone, or set
``AGENTREGISTRY_REF=<tag-or-sha>`` to bump the pinned ref.

Called by ``make build-registry``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "https://github.com/agentregistry-dev/agentregistry.git"
DEFAULT_REF = "v0.3.3"

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "services" / "agentregistry"
UPSTREAM_DIR = SERVICE_DIR / "upstream"
VENDORED_DOCKERFILE = SERVICE_DIR / "server.Dockerfile"
STAGED_DOCKERFILE = UPSTREAM_DIR / "server.Dockerfile"


def _clone(ref: str) -> int:
    UPSTREAM_DIR.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1", "--branch", ref, REPO, str(UPSTREAM_DIR)]
    print(" ".join(cmd))
    return subprocess.call(cmd)


def _stage_dockerfile() -> None:
    if not VENDORED_DOCKERFILE.is_file():
        raise SystemExit(f"missing vendored Dockerfile: {VENDORED_DOCKERFILE}")
    shutil.copy2(VENDORED_DOCKERFILE, STAGED_DOCKERFILE)
    print(f"staged {STAGED_DOCKERFILE.relative_to(ROOT)} "
          f"from {VENDORED_DOCKERFILE.relative_to(ROOT)}")


def main() -> int:
    ref = os.environ.get("AGENTREGISTRY_REF", DEFAULT_REF)

    if not UPSTREAM_DIR.exists():
        rc = _clone(ref)
        if rc != 0:
            print(f"clone failed (rc={rc})", file=sys.stderr)
            return rc
    else:
        print(f"{UPSTREAM_DIR.relative_to(ROOT)} exists — leaving in place "
              f"(delete to force re-clone)")

    # Always (re)stage the Dockerfile so edits to the vendored copy take
    # effect on the next build without requiring a re-clone.
    _stage_dockerfile()
    return 0


if __name__ == "__main__":
    sys.exit(main())
