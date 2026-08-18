#!/usr/bin/env python3
"""Export/import Open Notebook model config and set defaults.

Uses Open Notebook's REST API:
  - GET /api/models
  - GET /api/models/defaults
  - PUT /api/models/defaults
  - POST /api/models
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import urllib.error
import urllib.request
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

DEFAULT_BASE_URL = "http://127.0.0.1:5055"
DEFAULT_CHAT_MODEL_NAME = "azure-strong"
DEFAULT_TOOLS_MODEL_NAME = "azure-strong"
DEFAULT_EMBEDDING_MODEL_NAME = "embeddings"


def parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def _request(
    method: str,
    base_url: str,
    endpoint: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    url = f"{base_url.rstrip('/')}{endpoint}"
    payload = json.dumps(body).encode() if body is not None else None
    req_headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=payload, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {endpoint} failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {endpoint} failed: {exc.reason}") from exc


def _build_headers(password: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if password:
        headers["Authorization"] = f"Bearer {password}"
    return headers


def list_models(base_url: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    models = _request("GET", base_url, "/api/models", headers=headers)
    if not isinstance(models, list):
        raise RuntimeError("Unexpected response for /api/models")
    return [m for m in models if isinstance(m, dict)]


def get_defaults(base_url: str, headers: dict[str, str]) -> dict[str, Any]:
    defaults = _request("GET", base_url, "/api/models/defaults", headers=headers)
    if not isinstance(defaults, dict):
        raise RuntimeError("Unexpected response for /api/models/defaults")
    return defaults


def update_defaults(base_url: str, headers: dict[str, str], payload: dict[str, str]) -> dict[str, Any]:
    result = _request("PUT", base_url, "/api/models/defaults", headers=headers, body=payload)
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected response for PUT /api/models/defaults")
    return result


def create_model(base_url: str, headers: dict[str, str], model: dict[str, Any]) -> dict[str, Any]:
    body = {
        "name": model["name"],
        "provider": model["provider"],
        "type": model["type"],
    }
    credential = model.get("credential")
    if credential:
        body["credential"] = credential
    created = _request("POST", base_url, "/api/models", headers=headers, body=body)
    if not isinstance(created, dict):
        raise RuntimeError("Unexpected response for POST /api/models")
    return created


def _model_id_to_name(models: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for model in models:
        model_id = model.get("id")
        model_name = model.get("name")
        if isinstance(model_id, str) and isinstance(model_name, str):
            mapping[model_id] = model_name
    return mapping


def _resolve_model_id(models: list[dict[str, Any]], ref: str) -> str:
    by_id = {str(m.get("id")): m for m in models if m.get("id")}
    if ref in by_id:
        return ref

    name_matches = [m for m in models if m.get("name") == ref]
    if len(name_matches) == 1:
        model_id = name_matches[0].get("id")
        if isinstance(model_id, str):
            return model_id
    if len(name_matches) > 1:
        raise RuntimeError(
            f"Model name '{ref}' is ambiguous. Use model id instead."
        )

    raise RuntimeError(f"Model '{ref}' was not found in Open Notebook.")


def _model_key(model: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(model.get("name", "")).strip(),
        str(model.get("provider", "")).strip(),
        str(model.get("type", "")).strip(),
    )


def cmd_export(args: argparse.Namespace) -> None:
    headers = _build_headers(args.password)
    models = list_models(args.base_url, headers)
    defaults = get_defaults(args.base_url, headers)
    id_to_name = _model_id_to_name(models)

    defaults_by_name: dict[str, str] = {}
    for key, value in defaults.items():
        if isinstance(value, str) and value:
            defaults_by_name[key] = id_to_name.get(value, value)

    output = {
        "version": 1,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "models": models,
        "defaults": defaults,
        "defaults_by_name": defaults_by_name,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2) + "\n")
    print(f"wrote {out} ({len(models)} models)")


def _build_defaults_refs_from_args_or_file(
    args: argparse.Namespace,
    file_defaults_refs: dict[str, str],
) -> dict[str, str]:
    return {
        "default_chat_model": args.chat_model or file_defaults_refs.get("default_chat_model", DEFAULT_CHAT_MODEL_NAME),
        "default_tools_model": args.tools_model or file_defaults_refs.get("default_tools_model", DEFAULT_TOOLS_MODEL_NAME),
        "default_embedding_model": args.embedding_model or file_defaults_refs.get("default_embedding_model", DEFAULT_EMBEDDING_MODEL_NAME),
    }


def cmd_import(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.input).read_text())
    if not isinstance(payload, dict):
        raise RuntimeError("Import file must contain a JSON object.")

    file_models = payload.get("models", [])
    if not isinstance(file_models, list):
        raise RuntimeError("Import file has invalid 'models' value.")

    headers = _build_headers(args.password)
    current_models = list_models(args.base_url, headers)

    existing_keys = {_model_key(m) for m in current_models}
    created = 0
    for model in file_models:
        if not isinstance(model, dict):
            continue
        key = _model_key(model)
        if key in existing_keys:
            continue
        if not all(key):
            continue
        create_model(args.base_url, headers, model)
        created += 1

    current_models = list_models(args.base_url, headers)

    if not args.apply_defaults:
        print(f"import complete ({created} model(s) created, defaults unchanged)")
        return

    file_defaults_by_name = payload.get("defaults_by_name", {})
    if not isinstance(file_defaults_by_name, dict):
        file_defaults_by_name = {}
    file_defaults = payload.get("defaults", {})
    if not isinstance(file_defaults, dict):
        file_defaults = {}

    file_default_refs: dict[str, str] = {}
    for slot in ("default_chat_model", "default_tools_model", "default_embedding_model"):
        raw = file_defaults_by_name.get(slot, file_defaults.get(slot))
        if isinstance(raw, str) and raw:
            file_default_refs[slot] = raw

    defaults_by_name = _build_defaults_refs_from_args_or_file(args, file_default_refs)
    defaults_to_apply: dict[str, str] = {}
    for key, model_ref in defaults_by_name.items():
        defaults_to_apply[key] = _resolve_model_id(current_models, model_ref)

    result = update_defaults(args.base_url, headers, defaults_to_apply)
    print(f"import complete ({created} model(s) created, defaults updated)")
    print(json.dumps(result, indent=2))


def cmd_set_defaults(args: argparse.Namespace) -> None:
    headers = _build_headers(args.password)
    current_models = list_models(args.base_url, headers)
    payload = {
        "default_chat_model": _resolve_model_id(current_models, args.chat_model),
        "default_tools_model": _resolve_model_id(current_models, args.tools_model),
        "default_embedding_model": _resolve_model_id(current_models, args.embedding_model),
    }
    result = update_defaults(args.base_url, headers, payload)
    print(json.dumps(result, indent=2))


def build_parser() -> argparse.ArgumentParser:
    env = parse_env(ENV_FILE)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=env.get("OPEN_NOTEBOOK_API_URL", DEFAULT_BASE_URL),
        help=f"Open Notebook API base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--password",
        default=env.get("OPEN_NOTEBOOK_PASSWORD", ""),
        help="Open Notebook API password (sent as Bearer token).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export models/defaults to JSON")
    export_parser.add_argument("--output", required=True, help="Output JSON file path.")
    export_parser.set_defaults(func=cmd_export)

    import_parser = subparsers.add_parser("import", help="Import models/defaults from JSON")
    import_parser.add_argument("--input", required=True, help="Input JSON file path.")
    import_parser.add_argument(
        "--apply-defaults",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Apply defaults after importing models (default: true).",
    )
    import_parser.add_argument("--chat-model", help="Default chat model name or id.")
    import_parser.add_argument("--tools-model", help="Default tools model name or id.")
    import_parser.add_argument("--embedding-model", help="Default embedding model name or id.")
    import_parser.set_defaults(func=cmd_import)

    defaults_parser = subparsers.add_parser(
        "set-defaults",
        help="Set default chat/tools/embedding models.",
    )
    defaults_parser.add_argument(
        "--chat-model",
        default=DEFAULT_CHAT_MODEL_NAME,
        help=f"Default chat model name or id (default: {DEFAULT_CHAT_MODEL_NAME}).",
    )
    defaults_parser.add_argument(
        "--tools-model",
        default=DEFAULT_TOOLS_MODEL_NAME,
        help=f"Default tools model name or id (default: {DEFAULT_TOOLS_MODEL_NAME}).",
    )
    defaults_parser.add_argument(
        "--embedding-model",
        default=DEFAULT_EMBEDDING_MODEL_NAME,
        help=f"Default embedding model name or id (default: {DEFAULT_EMBEDDING_MODEL_NAME}).",
    )
    defaults_parser.set_defaults(func=cmd_set_defaults)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
