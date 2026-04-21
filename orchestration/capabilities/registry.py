"""规划与运行时绑定使用的能力目录默认值与注册表视图。"""

from __future__ import annotations

from typing import Any, Dict, List

from orchestration.capabilities.bindings import merge_capability_bindings

CAPABILITY_SOURCES = {"builtin", "skill_mapped", "custom"}

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
    "deleted_catalog_ids": [],
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
    source = str(entry.get("source") or "custom").strip().lower()
    if source not in CAPABILITY_SOURCES:
        source = "custom"
    return {
        "id": capability_id,
        "source": source,
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


def _infer_capability_category(capability_id: str) -> str:
    namespace = str(capability_id or "").split(":", 1)[0].split(".", 1)[0].strip().lower()
    if namespace in {"asset", "assets"}:
        return "assets"
    if namespace in {"doc", "document", "documents"}:
        return "documents"
    if namespace in {"planning", "plan"}:
        return "planning"
    if namespace in {"analysis", "requirements"}:
        return "analysis"
    if namespace in {"architecture", "design"}:
        return "design"
    if namespace in {"code", "coding", "build", "dev"}:
        return "engineering"
    if namespace in {"test", "testing", "qa", "quality", "review"}:
        return "quality"
    if namespace in {"delivery", "docs", "release"}:
        return "delivery"
    return "general"


def _infer_capability_binding_types(capability_id: str, category: str) -> List[str]:
    if category == "planning":
        return []
    if capability_id == "asset.generate:v1" or category == "assets":
        return ["direct_model", "http_api", "workflow_api", "internal_tool", "mcp_server"]
    if category == "documents":
        return ["http_api", "workflow_api", "internal_tool", "mcp_server"]
    return ["internal_tool", "mcp_server", "http_api", "workflow_api"]


def _infer_capability_outputs(capability_id: str, category: str) -> List[str]:
    if capability_id == "code.edit:v1" or category == "engineering":
        return ["diff", "patch", "json"]
    if capability_id == "test.run:v1" or category == "quality":
        return ["test_report", "log", "json"]
    if capability_id == "delivery.readme:v1" or category == "delivery":
        return ["md", "json"]
    if category == "assets":
        return ["asset", "asset_prompt", "json"]
    if category == "documents":
        return ["docx", "doc", "txt", "json"]
    return ["json"]


def _infer_capability_runtime_defaults(capability_id: str, category: str) -> Dict[str, Any]:
    if capability_id == "asset.generate:v1" or category == "assets":
        return {"output_formats": ["png"]}
    if capability_id == "doc.write:v1":
        return {"output_formats": ["docx"], "target_filename": "documents/output.docx"}
    if capability_id == "doc.read:v1":
        return {"source_path": ""}
    if capability_id == "delivery.readme:v1":
        return {"target_filename": "README.md"}
    if capability_id == "test.run:v1":
        return {"command": "pytest -q"}
    return {}


def _infer_capability_invocation_hint(capability_id: str, category: str) -> str:
    if capability_id == "code.edit:v1":
        return "输入目标文件、修改意图或补丁，返回代码差异、补丁结果或外部执行请求。"
    if capability_id == "test.run:v1":
        return "输入测试命令、范围或目标路径，返回测试结果、失败证据和日志。"
    if capability_id == "delivery.readme:v1":
        return "输入交付摘要、运行方式和限制说明，返回 README 或交付说明文档。"
    if category == "assets":
        return "输入素材清单、提示词和输出格式，返回素材文件、URL 或外部执行请求。"
    if category == "documents":
        return "输入文档路径或内容与目标文件名，返回文档产物、抽取文本或外部执行请求。"
    if category == "planning":
        return "输入规划约束和上下文，返回结构化流程建议或审查结论。"
    return "输入执行所需参数，返回结构化结果、产物或外部执行请求。"


def _build_skill_backfilled_capability(capability_id: str, stage_types: List[str] | None = None) -> Dict[str, Any]:
    category = _infer_capability_category(capability_id)
    normalized_stage_types = [str(item).strip() for item in (stage_types or []) if str(item).strip()]
    return {
        "id": capability_id,
        "source": "skill_mapped",
        "label": capability_id,
        "category": category,
        "description": "由 skill 引用自动补齐的能力定义，可在能力执行页继续补充绑定、参数与说明。",
        "enabled": True,
        "recommended_stage_types": normalized_stage_types,
        "default_stage_assignment": False,
        "planner_visible": True,
        "handler": capability_id,
        "required_model_kind": "image" if category == "assets" else "",
        "runtime_defaults": _infer_capability_runtime_defaults(capability_id, category),
        "produces_artifact_types": _infer_capability_outputs(capability_id, category),
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "properties": {}},
        "invocation_hint": _infer_capability_invocation_hint(capability_id, category),
        "supported_binding_types": _infer_capability_binding_types(capability_id, category),
    }


def merge_capability_settings(raw: Dict[str, Any] | None = None) -> Dict[str, Any]:
    merged = dict(DEFAULT_CAPABILITY_SETTINGS)
    source = raw if isinstance(raw, dict) else {}
    merged["vector_model"] = str(source.get("vector_model") or merged.get("vector_model") or "")
    merged["rerank_model"] = str(source.get("rerank_model") or merged.get("rerank_model") or "")
    merged["notes"] = str(source.get("notes") or merged.get("notes") or "")
    merged["bindings"] = merge_capability_bindings(source.get("bindings"))
    deleted_catalog_ids = source.get("deleted_catalog_ids")
    if not isinstance(deleted_catalog_ids, list):
        deleted_catalog_ids = []
    deleted_set = {str(item).strip() for item in deleted_catalog_ids if str(item).strip()}
    merged["deleted_catalog_ids"] = sorted(deleted_set)

    catalog: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    ordered_ids: List[str] = []
    explicit_ids: set[str] = set()
    if isinstance(source.get("catalog"), list):
        for item in source.get("catalog") or []:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_capability_entry(item)
            if not normalized:
                continue
            explicit_ids.add(normalized["id"])
    for item in DEFAULT_CAPABILITY_CATALOG:
        normalized = _normalize_capability_entry(item)
        if not normalized:
            continue
        normalized["source"] = "builtin"
        by_id[normalized["id"]] = dict(normalized)
        ordered_ids.append(normalized["id"])
    if isinstance(source.get("catalog"), list):
        for item in source.get("catalog") or []:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_capability_entry(item)
            if not normalized:
                continue
            if normalized["id"] in ordered_ids:
                normalized["source"] = "builtin"
            elif normalized.get("source") != "skill_mapped":
                normalized["source"] = "custom"
            if normalized["id"] not in ordered_ids:
                ordered_ids.append(normalized["id"])
            by_id[normalized["id"]] = normalized
    for capability_id in ordered_ids:
        if capability_id not in by_id:
            continue
        catalog.append(by_id[capability_id])
    merged["catalog"] = catalog
    return merged


def sync_capability_settings_with_skills(
    capability_settings: Dict[str, Any] | None = None,
    skill_settings: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    merged = merge_capability_settings(capability_settings)
    skills = skill_settings if isinstance(skill_settings, dict) else {}
    deleted_set = {str(item).strip() for item in (merged.get("deleted_catalog_ids") or []) if str(item).strip()}
    catalog = [dict(item) for item in merged.get("catalog") or [] if isinstance(item, dict)]
    by_id: Dict[str, Dict[str, Any]] = {str(item.get("id") or "").strip(): item for item in catalog if str(item.get("id") or "").strip()}

    for skill in skills.get("catalog") or []:
        if not isinstance(skill, dict):
            continue
        stage_types = [str(item).strip() for item in (skill.get("recommended_stage_types") or []) if str(item).strip()]
        capability_ids = []
        capability_ids.extend(skill.get("preferred_capabilities") or [])
        capability_ids.extend(skill.get("required_capabilities") or [])
        for raw_capability_id in capability_ids:
            capability_id = str(raw_capability_id or "").strip()
            if not capability_id or capability_id in deleted_set:
                continue
            if capability_id not in by_id:
                entry = _build_skill_backfilled_capability(capability_id, stage_types=stage_types)
                catalog.append(entry)
                by_id[capability_id] = entry
                continue
            existing = by_id[capability_id]
            existing_stage_types = [str(item).strip() for item in (existing.get("recommended_stage_types") or []) if str(item).strip()]
            combined_stage_types: List[str] = []
            for stage_type in existing_stage_types + stage_types:
                if stage_type and stage_type not in combined_stage_types:
                    combined_stage_types.append(stage_type)
            if combined_stage_types:
                existing["recommended_stage_types"] = combined_stage_types

    merged["catalog"] = catalog
    return merge_capability_settings(merged)


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
                "source": item.get("source") or "custom",
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
    "sync_capability_settings_with_skills",
]
