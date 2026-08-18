#!/usr/bin/env python3
"""Tool-usage statistics from Phoenix traces.

Queries the Phoenix REST API to aggregate tool invocation data across all
projects (agents).  Designed to run from the host — connects to Phoenix via
Caddy at http://phoenix.localhost (port 80) by default.

Usage:
    python3 scripts/tool_stats.py                  # last 7 days
    python3 scripts/tool_stats.py --days 30         # last 30 days
    python3 scripts/tool_stats.py --json            # machine-readable
    python3 scripts/tool_stats.py --phoenix-url http://localhost:6006
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PHOENIX_URL = os.environ.get("PHOENIX_URL", "http://phoenix.localhost")
PAGE_SIZE = 100
SPAN_KINDS_OF_INTEREST = {"TOOL", "AGENT", "CHAIN"}


# ── HTTP helpers ──────────────────────────────────────────────────────────

def _client(base_url: str) -> httpx.Client:
    """Create an httpx client that bypasses the corporate proxy for localhost."""
    return httpx.Client(
        base_url=base_url,
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        # Bypass proxy for local traffic
        proxy=None,
    )


def _get_json(client: httpx.Client, path: str, **params: Any) -> dict:
    """GET with pagination params and error handling."""
    resp = client.get(path, params={k: v for k, v in params.items() if v is not None},
                      headers={"accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


# ── Phoenix REST API wrappers ──────────────────────────────────────────────

def list_projects(client: httpx.Client) -> list[dict]:
    """Return all Phoenix projects via GET /v1/projects."""
    projects: list[dict] = []
    cursor = None
    while True:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        data = _get_json(client, "v1/projects", **params)
        projects.extend(data.get("data", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return projects


def get_spans(
    client: httpx.Client,
    project_id: str,
    *,
    span_kind: str | list[str] | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 1000,
) -> list[dict]:
    """Fetch spans for a project via GET /v1/projects/{id}/spans."""
    all_spans: list[dict] = []
    cursor = None
    while len(all_spans) < limit:
        page_size = min(PAGE_SIZE, limit - len(all_spans))
        params: dict[str, Any] = {"limit": page_size}
        if start_time:
            params["start_time"] = start_time.isoformat()
        if end_time:
            params["end_time"] = end_time.isoformat()
        if span_kind:
            params["span_kind"] = [span_kind] if isinstance(span_kind, str) else span_kind
        if cursor:
            params["cursor"] = cursor

        data = _get_json(client, f"v1/projects/{quote(project_id, safe='')}/spans", **params)
        spans = data.get("data", [])
        all_spans.extend(spans)
        cursor = data.get("next_cursor")
        if not cursor or not spans:
            break
    return all_spans[:limit]


# ── Analysis ──────────────────────────────────────────────────────────────

def _duration_ms(span: dict) -> float | None:
    """Calculate span duration in milliseconds from ISO timestamps."""
    start = span.get("start_time")
    end = span.get("end_time")
    if not start or not end:
        return None
    try:
        t0 = datetime.fromisoformat(start)
        t1 = datetime.fromisoformat(end)
        return (t1 - t0).total_seconds() * 1000
    except (ValueError, TypeError):
        return None


def _tool_name(span: dict) -> str:
    """Extract the tool name from span attributes or fall back to span name."""
    attrs = span.get("attributes", {})
    # Our agents set tool.name explicitly
    if isinstance(attrs, dict):
        name = attrs.get("tool.name")
        if name:
            return name
    return span.get("name", "unknown")


def _is_error(span: dict) -> bool:
    """Check if a span has error status."""
    status = span.get("status", {})
    if isinstance(status, dict):
        return status.get("status_code") == "ERROR" or status.get("code") == "ERROR"
    return False


def analyse_project(client: httpx.Client, project: dict, since: datetime) -> dict | None:
    """Gather stats for a single project (agent)."""
    pid = project.get("id") or project.get("name", "")
    name = project.get("name", pid)

    # Fetch TOOL spans for tool-level stats
    tool_spans = get_spans(client, pid, span_kind="TOOL", start_time=since, limit=5000)

    # Fetch AGENT spans for invocation counts
    agent_spans = get_spans(client, pid, span_kind="AGENT", start_time=since, limit=5000)

    if not tool_spans and not agent_spans:
        return None

    # Aggregate tool calls
    tool_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "durations_ms": []}
    )
    for span in tool_spans:
        tname = _tool_name(span)
        tool_stats[tname]["calls"] += 1
        if _is_error(span):
            tool_stats[tname]["errors"] += 1
        dur = _duration_ms(span)
        if dur is not None:
            tool_stats[tname]["durations_ms"].append(dur)

    # Summarise per-tool
    tool_summary = {}
    for tname, info in sorted(tool_stats.items(), key=lambda x: -x[1]["calls"]):
        durs = info["durations_ms"]
        tool_summary[tname] = {
            "calls": info["calls"],
            "errors": info["errors"],
            "avg_ms": round(sum(durs) / len(durs), 1) if durs else None,
            "p95_ms": round(sorted(durs)[int(len(durs) * 0.95)] if durs else 0, 1),
        }

    return {
        "project": name,
        "agent_invocations": len(agent_spans),
        "total_tool_calls": len(tool_spans),
        "unique_tools": len(tool_stats),
        "error_rate": round(
            sum(t["errors"] for t in tool_stats.values()) / max(len(tool_spans), 1) * 100, 1
        ),
        "tools": tool_summary,
    }


# ── Output formatting ────────────────────────────────────────────────────

def _print_table(results: list[dict], days: int) -> None:
    """Pretty-print results to the terminal."""
    now = datetime.now(timezone.utc)
    print(f"\n📊  Tool Usage Stats  (last {days} days, as of {now:%Y-%m-%d %H:%M} UTC)\n")

    if not results:
        print("  No trace data found. Is the stack running? Have you used any tools?")
        print(f"  Checked Phoenix at: {DEFAULT_PHOENIX_URL}")
        return

    # Summary table
    print(f"{'Agent':<22} {'Invocations':>12} {'Tool Calls':>12} {'Error %':>9}")
    print("─" * 58)
    total_invocations = 0
    total_tool_calls = 0
    for r in results:
        total_invocations += r["agent_invocations"]
        total_tool_calls += r["total_tool_calls"]
        print(
            f"  {r['project']:<20} {r['agent_invocations']:>10} "
            f"{r['total_tool_calls']:>12} {r['error_rate']:>8.1f}%"
        )
    print("─" * 58)
    print(f"  {'TOTAL':<20} {total_invocations:>10} {total_tool_calls:>12}")

    # Per-tool breakdown
    print(f"\n{'Tool':<28} {'Calls':>8} {'Errors':>8} {'Avg ms':>10} {'P95 ms':>10}  Agent")
    print("─" * 82)
    for r in results:
        for tname, info in r["tools"].items():
            avg = f"{info['avg_ms']:.0f}" if info["avg_ms"] is not None else "—"
            p95 = f"{info['p95_ms']:.0f}" if info["p95_ms"] else "—"
            print(
                f"  {tname:<26} {info['calls']:>6} {info['errors']:>8} "
                f"{avg:>10} {p95:>10}  {r['project']}"
            )

    print()


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Tool-usage stats from Phoenix traces")
    parser.add_argument("--phoenix-url", default=DEFAULT_PHOENIX_URL,
                        help="Phoenix base URL (default: %(default)s)")
    parser.add_argument("--days", type=int, default=7,
                        help="Look-back window in days (default: 7)")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="Output as JSON instead of a table")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    try:
        client = _client(args.phoenix_url)
        # Quick connectivity check
        client.get("v1/projects", params={"limit": 1},
                    headers={"accept": "application/json"}).raise_for_status()
    except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
        print(f"❌  Cannot reach Phoenix at {args.phoenix_url}", file=sys.stderr)
        print(f"    {exc}", file=sys.stderr)
        print("    Is the stack running? (make up)", file=sys.stderr)
        sys.exit(1)

    projects = list_projects(client)
    results: list[dict] = []
    for project in projects:
        stats = analyse_project(client, project, since)
        if stats:
            results.append(stats)

    results.sort(key=lambda r: -r["total_tool_calls"])

    if args.json_out:
        print(json.dumps(results, indent=2))
    else:
        _print_table(results, args.days)


if __name__ == "__main__":
    main()
