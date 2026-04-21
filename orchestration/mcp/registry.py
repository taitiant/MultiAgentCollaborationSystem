from __future__ import annotations

from typing import Any, Dict, List


DEFAULT_MCP_SETTINGS: Dict[str, Any] = {
    "mcpServers": {},
}


def _normalize_string(value: Any) -> str:
    return str(value or "").strip()


def _normalize_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_list_of_strings(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _merge_nested(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_nested(merged.get(key) or {}, value)
        else:
            merged[key] = value
    return merged


def _is_meaningful(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def normalize_mcp_server_entry(name: str, entry: Dict[str, Any]) -> Dict[str, Any] | None:
    server_name = _normalize_string(name)
    if not server_name or not isinstance(entry, dict):
        return None
    command = _normalize_string(entry.get("command"))
    url = _normalize_string(entry.get("url"))
    transport = _normalize_string(entry.get("transport"))
    if not transport:
        transport = "streamable_http" if url else "stdio"
    return {
        "name": server_name,
        "enabled": bool(entry.get("enabled", True)),
        "transport": transport,
        "command": command,
        "args": _normalize_list_of_strings(entry.get("args")),
        "env": _normalize_dict(entry.get("env")),
        "cwd": _normalize_string(entry.get("cwd")),
        "url": url,
        "headers": _normalize_dict(entry.get("headers")),
        "description": _normalize_string(entry.get("description")),
    }


def merge_mcp_settings(raw: Dict[str, Any] | None = None) -> Dict[str, Any]:
    merged = dict(DEFAULT_MCP_SETTINGS)
    source = raw if isinstance(raw, dict) else {}
    raw_servers = source.get("mcpServers")
    if not isinstance(raw_servers, dict):
        raw_servers = source.get("servers")
    normalized_servers: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw_servers, dict):
        for name, entry in raw_servers.items():
            normalized = normalize_mcp_server_entry(str(name), entry if isinstance(entry, dict) else {})
            if normalized:
                normalized_servers[normalized["name"]] = normalized
    merged["mcpServers"] = normalized_servers
    return merged


def get_mcp_servers(settings: Dict[str, Any] | None = None) -> Dict[str, Dict[str, Any]]:
    merged = merge_mcp_settings(settings)
    raw = merged.get("mcpServers")
    return dict(raw) if isinstance(raw, dict) else {}


def resolve_mcp_server(server_name: str, settings: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
    target = _normalize_string(server_name)
    if not target:
        return None
    server = get_mcp_servers(settings).get(target)
    return dict(server) if isinstance(server, dict) else None


def expand_mcp_binding(mcp_binding: Dict[str, Any] | None, settings: Dict[str, Any] | None = None) -> Dict[str, Any]:
    binding = dict(mcp_binding or {})
    server_name = _normalize_string(binding.get("server_name") or binding.get("name"))
    server = resolve_mcp_server(server_name, settings)
    if not server:
        return binding
    base = {
        "server_name": server.get("name") or server_name,
        "transport": server.get("transport") or "stdio",
        "launch": {
            "command": _normalize_string(server.get("command")),
            "args": _normalize_list_of_strings(server.get("args")),
            "env": _normalize_dict(server.get("env")),
            "cwd": _normalize_string(server.get("cwd")),
        },
        "endpoint": {
            "url": _normalize_string(server.get("url")),
            "headers": _normalize_dict(server.get("headers")),
        },
        "tools": [],
        "timeout_sec": int(server.get("timeout_sec") or 120),
    }
    merged = dict(base)
    for key, value in binding.items():
        if key in {"launch", "endpoint"} and isinstance(value, dict):
            next_nested = dict(merged.get(key) or {})
            for nested_key, nested_value in value.items():
                if _is_meaningful(nested_value):
                    next_nested[nested_key] = nested_value
            merged[key] = next_nested
            continue
        if key == "tools" and isinstance(value, list) and value:
            merged[key] = value
            continue
        if _is_meaningful(value):
            merged[key] = value
    return merged


__all__ = [
    "DEFAULT_MCP_SETTINGS",
    "expand_mcp_binding",
    "get_mcp_servers",
    "merge_mcp_settings",
    "normalize_mcp_server_entry",
    "resolve_mcp_server",
]
