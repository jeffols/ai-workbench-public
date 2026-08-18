#!/usr/bin/env python3
"""Send queries to the AI Workbench from the command line.

Routes through Open WebUI's chat completions API, which processes MCP tool
calls server-side — so the full tool chain (code-agent, magic-fetch,
mcp-fetch, mcp-time, etc.) is available.

Usage:
    python3 scripts/query.py "Fetch https://example.com and summarize it"
    python3 scripts/query.py -m azure-strong "What time is it in Tokyo?"
    echo "Analyze this CSV and chart the trend" | python3 scripts/query.py

Requires OPENWEBUI_API_KEY — generate one in Open WebUI:
Settings → Account → API Keys, then add to .env.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://localhost:3000"
TIMEOUT = 300  # generous — tool calls can take a while


def _read_env(key: str) -> str | None:
    """Read a key from the environment or the .env file."""
    val = os.environ.get(key)
    if val:
        return val
    env_file = WORKSPACE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}="):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _get_api_key() -> str | None:
    return _read_env("OPENWEBUI_API_KEY")


def _get_default_model() -> str:
    return _read_env("DEFAULT_CHAT_MODEL") or "azure-fast"


def _stream(query: str, model: str, api_key: str, base_url: str) -> None:
    """Stream a chat completion via SSE."""
    parsed = urlparse(base_url)
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": query}],
        "stream": True,
    })

    if parsed.scheme == "https":
        import ssl
        conn = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443,
            context=ssl.create_default_context(), timeout=TIMEOUT,
        )
    else:
        conn = http.client.HTTPConnection(
            parsed.hostname, parsed.port or 80, timeout=TIMEOUT,
        )

    try:
        conn.request(
            "POST",
            "/api/chat/completions",
            body=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        resp = conn.getresponse()

        if resp.status != 200:
            body = resp.read().decode(errors="replace")[:500]
            print(f"Error {resp.status}: {body}", file=sys.stderr)
            sys.exit(1)

        for raw_line in resp:
            line = raw_line.decode(errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    sys.stdout.write(content)
                    sys.stdout.flush()
            except json.JSONDecodeError:
                continue
        print()  # trailing newline
    finally:
        conn.close()


def _no_stream(query: str, model: str, api_key: str, base_url: str) -> None:
    """Non-streaming chat completion (waits for full response)."""
    parsed = urlparse(base_url)
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": query}],
    })

    if parsed.scheme == "https":
        import ssl
        conn = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443,
            context=ssl.create_default_context(), timeout=TIMEOUT,
        )
    else:
        conn = http.client.HTTPConnection(
            parsed.hostname, parsed.port or 80, timeout=TIMEOUT,
        )

    try:
        conn.request(
            "POST",
            "/api/chat/completions",
            body=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp = conn.getresponse()
        body = resp.read().decode(errors="replace")

        if resp.status != 200:
            print(f"Error {resp.status}: {body[:500]}", file=sys.stderr)
            sys.exit(1)

        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
        print(content)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query the AI Workbench from the command line.",
        epilog=(
            "Requires OPENWEBUI_API_KEY (env var or in .env). "
            "Generate one in Open WebUI: Settings → Account → API Keys."
        ),
    )
    parser.add_argument("query", nargs="*", help="The question to ask")
    parser.add_argument(
        "-m", "--model",
        help=f"Model alias (default: DEFAULT_CHAT_MODEL from .env)",
    )
    parser.add_argument(
        "--url", default=DEFAULT_URL,
        help=f"Open WebUI base URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--no-stream", action="store_true",
        help="Wait for full response instead of streaming",
    )
    args = parser.parse_args()

    # Query from args or stdin
    if args.query:
        query = " ".join(args.query)
    elif not sys.stdin.isatty():
        query = sys.stdin.read().strip()
    else:
        parser.error("Provide a query as an argument or pipe via stdin")

    if not query:
        parser.error("Query is empty")

    api_key = _get_api_key()
    if not api_key:
        print(
            "OPENWEBUI_API_KEY not found.\n"
            "Generate one in Open WebUI: Settings → Account → API Keys.\n"
            "Then add to .env:  OPENWEBUI_API_KEY=sk-...",
            file=sys.stderr,
        )
        sys.exit(1)

    model = args.model or _get_default_model()

    if args.no_stream:
        _no_stream(query, model, api_key, args.url)
    else:
        _stream(query, model, api_key, args.url)


if __name__ == "__main__":
    main()
