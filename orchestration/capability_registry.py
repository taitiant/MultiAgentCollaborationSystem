from __future__ import annotations

from typing import Any, Dict, List

from orchestration.capability_bindings import merge_capability_bindings

DEFAULT_CAPABILITY_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "asset.generate:v1",
        "label": "素材生成",
        "category": "assets",
        "description": "通过模型、HTTP API、工作流或内部工具生成素材文件。",
        "recommended_stage_types": ["assets"],
        "default_stage_assignment": False,
        "planner_visible": True,
        "invocation_hint": "输入素材清单、提示词和输出格式，返回素材文件、URL 或外部执行请求。",
        "input_schema": {
            "type": "object",
            "properties": {
                "assets": {"type": "array"},
                "asset_mode": {"type": "string"},
                "output_formats": {"type": "array"},
            },
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "assets": {"type": "array"},
                "request_manifest": {"type": "object"},
            },
        },
        "supported_binding_types": ["direct_model", "http_api", "workflow_api", "internal_tool", "mcp_server"],
    },
    {
        "id": "doc.read:v1",
        "label": "文档读取",
        "category": "documents",
        "description": "读取 doc/docx 文档正文并转成可继续处理的文本产物；内置优先支持 docx。",
        "recommended_stage_types": ["requirements", "architecture", "docs"],
        "default_stage_assignment": False,
        "planner_visible": True,
        "invocation_hint": "输入 doc/docx 文件路径，输出抽取后的正文文本；doc 默认建议走外部适配器。",
        "input_schema": {
            "type": "object",
            "required": ["source_path"],
            "properties": {
                "source_path": {"type": "string"},
                "extract_mode": {"type": "string"},
            },
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "output_path": {"type": "string"},
            },
        },
        "supported_binding_types": ["http_api", "workflow_api", "internal_tool", "mcp_server"],
    },
    {
        "id": "doc.write:v1",
        "label": "文档写入",
        "category": "documents",
        "description": "把结构化文本写成 doc/docx 文档；内置优先支持 docx，doc 可通过 HTTP API / 工作流 / 工具绑定执行。",
        "recommended_stage_types": ["docs"],
        "default_stage_assignment": False,
        "planner_visible": True,
        "invocation_hint": "输入正文内容、目标文件名和输出格式，返回 docx 产物或外部写入请求。",
        "input_schema": {
            "type": "object",
            "required": ["content"],
            "properties": {
                "content": {"type": "string"},
                "target_filename": {"type": "string"},
                "output_formats": {"type": "array"},
            },
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "artifacts": {"type": "array"},
                "request_manifest": {"type": "object"},
            },
        },
        "supported_binding_types": ["http_api", "workflow_api", "internal_tool", "mcp_server"],
    },
]

DEFAULT_CAPABILITY_SETTINGS: Dict[str, Any] = {
    "vector_model": "",
    "rerank_model": "",
    "notes": "这里只配置真正需要工具、插件、模块或外部接口参与执行的能力。",
    "catalog": DEFAULT_CAPABILITY_CATALOG,
    "bindings": [],
}


def get_default_capability_catalog() -> List[Dict[str, Any]]:
    return [dict(item) for item in merge_capability_settings().get("catalog") or [] if isinstance(item, dict)]


def _normalize_capability_entry(entry: Dict[str, Any]) -> Dict[str, Any] | None:
    capability_id = str(entry.get("id") or "").strip()
    if not capability_id:
        return None
    stage_types = entry.get("recommended_stage_types")
    if not isinstance(stage_types, list):
        stage_types = []
    return {
        "id": capability_id,
        "label": str(entry.get("label") or capability_id).strip() or capability_id,
        "category": str(entry.get("category") or "general").strip() or "general",
        "description": str(entry.get("description") or "").strip(),
        "enabled": bool(entry.get("enabled", True)),
        "recommended_stage_types": [str(item).strip() for item in stage_types if str(item).strip()],
        "default_stage_assignment": bool(entry.get("default_stage_assignment", False)),
        "planner_visible": bool(entry.get("planner_visible", True)),
        "handler": str(entry.get("handler") or capability_id).strip() or capability_id,
        "required_model_kind": str(entry.get("required_model_kind") or "").strip(),
        "runtime_defaults": dict(entry.get("runtime_defaults") or {}) if isinstance(entry.get("runtime_defaults"), dict) else {},
        "produces_artifact_types": [str(item).strip() for item in (entry.get("produces_artifact_types") or []) if str(item).strip()] if isinstance(entry.get("produces_artifact_types"), list) else [],
        "input_schema": dict(entry.get("input_schema") or {}) if isinstance(entry.get("input_schema"), dict) else {},
        "output_schema": dict(entry.get("output_schema") or {}) if isinstance(entry.get("output_schema"), dict) else {},
        "invocation_hint": str(entry.get("invocation_hint") or "").strip(),
        "supported_binding_types": [str(item).strip() for item in (entry.get("supported_binding_types") or []) if str(item).strip()] if isinstance(entry.get("supported_binding_types"), list) else [],
    }


def merge_capability_settings(raw: Dict[str, Any] | None = None) -> Dict[str, Any]:
    merged = dict(DEFAULT_CAPABILITY_SETTINGS)
    source = raw if isinstance(raw, dict) else {}
    merged["vector_model"] = str(source.get("vector_model") or merged.get("vector_model") or "")
    merged["rerank_model"] = str(source.get("rerank_model") or merged.get("rerank_model") or "")
    merged["notes"] = str(source.get("notes") or merged.get("notes") or "")
    merged["bindings"] = merge_capability_bindings(source.get("bindings"))

    catalog: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    ordered_ids: List[str] = []
    for item in DEFAULT_CAPABILITY_CATALOG:
        normalized = _normalize_capability_entry(item)
        if not normalized:
            continue
        by_id[normalized["id"]] = dict(normalized)
        ordered_ids.append(normalized["id"])
    if isinstance(source.get("catalog"), list):
        for item in source.get("catalog") or []:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_capability_entry(item)
            if not normalized:
                continue
            if normalized["id"] not in ordered_ids:
                ordered_ids.append(normalized["id"])
            by_id[normalized["id"]] = normalized
    for capability_id in ordered_ids:
        if capability_id not in by_id:
            continue
        catalog.append(by_id[capability_id])
    merged["catalog"] = catalog
    return merged


def get_capability_catalog(raw: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    settings = merge_capability_settings(raw)
    return [dict(item) for item in settings.get("catalog") or [] if isinstance(item, dict)]


def get_capability_index(raw: Dict[str, Any] | None = None) -> Dict[str, Dict[str, Any]]:
    return {item["id"]: item for item in get_capability_catalog(raw)}


def default_capabilities_for_stage(stage_type: str, raw: Dict[str, Any] | None = None) -> List[str]:
    normalized = str(stage_type or "").strip()
    capabilities: List[str] = []
    for item in get_capability_catalog(raw):
        if not item.get("enabled", True):
            continue
        if not item.get("default_stage_assignment"):
            continue
        if normalized not in (item.get("recommended_stage_types") or []):
            continue
        capabilities.append(item["id"])
    return capabilities


def capability_prompt_view(raw: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    view: List[Dict[str, Any]] = []
    for item in get_capability_catalog(raw):
        if not item.get("enabled", True):
            continue
        if not item.get("planner_visible", True):
            continue
        view.append(
            {
                "id": item["id"],
                "label": item.get("label"),
                "category": item.get("category"),
                "description": item.get("description"),
                "enabled": bool(item.get("enabled", True)),
                "recommended_stage_types": item.get("recommended_stage_types") or [],
                "default_stage_assignment": bool(item.get("default_stage_assignment", False)),
                "invocation_hint": item.get("invocation_hint") or "",
                "input_fields": list(((item.get("input_schema") or {}).get("properties") or {}).keys())[:8] if isinstance(item.get("input_schema"), dict) else [],
                "output_fields": list(((item.get("output_schema") or {}).get("properties") or {}).keys())[:8] if isinstance(item.get("output_schema"), dict) else [],
                "supported_binding_types": item.get("supported_binding_types") or [],
            }
        )
    return view


__all__ = [
    "DEFAULT_CAPABILITY_CATALOG",
    "DEFAULT_CAPABILITY_SETTINGS",
    "capability_prompt_view",
    "default_capabilities_for_stage",
    "get_default_capability_catalog",
    "get_capability_catalog",
    "get_capability_index",
    "merge_capability_settings",
]
