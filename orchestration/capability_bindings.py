from __future__ import annotations

from typing import Any, Dict, List, Optional


BINDING_TYPES = {"direct_model", "http_api", "workflow_api", "internal_tool", "mcp_server"}


def _normalize_string(value: Any) -> str:
    return str(value or "").strip()


def _normalize_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_list_of_strings(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_mcp_tool_sequence(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tool_name = _normalize_string(item.get("name") or item.get("tool"))
        if not tool_name:
            continue
        normalized.append(
            {
                "name": tool_name,
                "args_template": _normalize_dict(item.get("args_template") or item.get("args")),
                "result_path": _normalize_string(item.get("result_path")),
                "required": bool(item.get("required", True)),
            }
        )
    return normalized


def _normalize_mcp_binding(value: Any) -> Dict[str, Any]:
    raw = _normalize_dict(value)
    launch = _normalize_dict(raw.get("launch"))
    endpoint = _normalize_dict(raw.get("endpoint"))
    return {
        "server_name": _normalize_string(raw.get("server_name") or raw.get("name")),
        "transport": _normalize_string(raw.get("transport") or "stdio") or "stdio",
        "launch": {
            "command": _normalize_string(launch.get("command")),
            "args": _normalize_list_of_strings(launch.get("args")),
            "env": _normalize_dict(launch.get("env")),
            "cwd": _normalize_string(launch.get("cwd")),
        },
        "endpoint": {
            "url": _normalize_string(endpoint.get("url")),
            "headers": _normalize_dict(endpoint.get("headers")),
        },
        "tools": _normalize_mcp_tool_sequence(raw.get("tools")),
        "timeout_sec": int(raw.get("timeout_sec") or 120),
    }


def normalize_capability_binding(entry: Dict[str, Any]) -> Dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    binding_id = _normalize_string(entry.get("id"))
    capability_id = _normalize_string(entry.get("capability_id"))
    if not binding_id or not capability_id:
        return None
    binding_type = _normalize_string(entry.get("binding_type") or entry.get("type") or "http_api").lower()
    if binding_type not in BINDING_TYPES:
        binding_type = "http_api"
    transport = _normalize_dict(entry.get("transport"))
    model = _normalize_dict(entry.get("model"))
    auth = _normalize_dict(entry.get("auth"))
    workflow = _normalize_dict(entry.get("workflow"))
    tool = _normalize_dict(entry.get("tool"))
    mcp = _normalize_mcp_binding(entry.get("mcp"))
    defaults = _normalize_dict(entry.get("defaults"))
    response_contract = _normalize_dict(entry.get("response_contract") or entry.get("response_spec"))
    tags = _normalize_list_of_strings(entry.get("tags"))
    return {
        "id": binding_id,
        "capability_id": capability_id,
        "label": _normalize_string(entry.get("label") or binding_id) or binding_id,
        "description": _normalize_string(entry.get("description")),
        "binding_type": binding_type,
        "enabled": bool(entry.get("enabled", True)),
        "priority": int(entry.get("priority") or 100),
        "required_model_kind": _normalize_string(entry.get("required_model_kind")),
        "transport": transport,
        "model": model,
        "auth": auth,
        "workflow": workflow,
        "tool": tool,
        "mcp": mcp,
        "defaults": defaults,
        "response_contract": response_contract,
        "tags": tags,
    }


def merge_capability_bindings(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    bindings: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    ordered_ids: List[str] = []
    for item in raw:
        normalized = normalize_capability_binding(item)
        if not normalized:
            continue
        binding_id = normalized["id"]
        if binding_id not in ordered_ids:
            ordered_ids.append(binding_id)
        by_id[binding_id] = normalized
    for binding_id in ordered_ids:
        current = by_id.get(binding_id)
        if current:
            bindings.append(current)
    return bindings


def get_capability_bindings(settings: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    raw = settings.get("bindings") if isinstance(settings, dict) else []
    return [dict(item) for item in merge_capability_bindings(raw)]


def get_capability_bindings_for_capability(capability_id: str, settings: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    target = _normalize_string(capability_id)
    bindings = [item for item in get_capability_bindings(settings) if item.get("capability_id") == target and item.get("enabled", True)]
    return sorted(bindings, key=lambda item: (int(item.get("priority") or 100), str(item.get("id") or "")))


def resolve_capability_binding(
    capability_id: str,
    settings: Dict[str, Any] | None = None,
    requested_binding_id: str | None = None,
) -> Optional[Dict[str, Any]]:
    requested = _normalize_string(requested_binding_id)
    bindings = get_capability_bindings_for_capability(capability_id, settings)
    if requested:
        for item in bindings:
            if item.get("id") == requested:
                return dict(item)
        return None
    if not bindings:
        return None
    return dict(bindings[0])


def binding_prompt_view(settings: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    view: List[Dict[str, Any]] = []
    for item in get_capability_bindings(settings):
        view.append(
            {
                "id": item.get("id"),
                "capability_id": item.get("capability_id"),
                "label": item.get("label"),
                "binding_type": item.get("binding_type"),
                "required_model_kind": item.get("required_model_kind"),
                "enabled": bool(item.get("enabled", True)),
                "priority": int(item.get("priority") or 100),
            }
        )
    return view


__all__ = [
    "BINDING_TYPES",
    "binding_prompt_view",
    "get_capability_bindings",
    "get_capability_bindings_for_capability",
    "merge_capability_bindings",
    "normalize_capability_binding",
    "resolve_capability_binding",
]
