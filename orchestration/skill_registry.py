from __future__ import annotations

from typing import Any, Dict, List


DEFAULT_SKILL_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "planning.dynamic_orchestration:v1",
        "label": "动态流程设计",
        "category": "planning",
        "description": "指导 leader 基于任务复杂度、能力供给和风险现场设计流程，而不是套用固定模板。",
        "recommended_stage_types": [],
        "default_stage_assignment": False,
        "planner_visible": True,
        "prompt_hint": "先判断任务复杂度、风险和缺口，再决定阶段数量、闭环结构、角色分工与人工决策点。",
        "preferred_capabilities": [],
        "required_capabilities": [],
    },
    {
        "id": "planning.review_governance:v1",
        "label": "规划评审与收敛",
        "category": "planning",
        "description": "指导 leader 或评审智能体基于证据判断方案是否通过、是否返工、是否需要人工决策。",
        "recommended_stage_types": [],
        "default_stage_assignment": False,
        "planner_visible": True,
        "prompt_hint": "优先依据需求、前置产物和失败证据给出通过/返工/人工决策判断，不要只给笼统建议。",
        "preferred_capabilities": [],
        "required_capabilities": [],
    },
    {
        "id": "requirements.discovery:v1",
        "label": "需求澄清方法",
        "category": "analysis",
        "description": "帮助需求分析智能体聚焦目标、范围、约束和验收标准，避免越界到架构或实现。",
        "recommended_stage_types": ["requirements"],
        "default_stage_assignment": True,
        "planner_visible": True,
        "prompt_hint": "先界定目标、边界、非功能约束和验收标准，再输出需求文档。",
        "preferred_capabilities": [],
        "required_capabilities": [],
    },
    {
        "id": "architecture.solutioning:v1",
        "label": "方案拆解方法",
        "category": "design",
        "description": "帮助架构智能体输出可实现的技术方案、模块职责和单一文件清单。",
        "recommended_stage_types": ["architecture"],
        "default_stage_assignment": True,
        "planner_visible": True,
        "prompt_hint": "先收敛到单一技术栈，再给出模块边界、依赖关系和唯一文件清单。",
        "preferred_capabilities": [],
        "required_capabilities": [],
    },
    {
        "id": "asset.prompting:v1",
        "label": "素材策划与提示词工程",
        "category": "assets",
        "description": "帮助素材智能体先做素材清单、规格拆解和提示词设计，再按需发起生成。",
        "recommended_stage_types": ["assets"],
        "default_stage_assignment": True,
        "planner_visible": True,
        "prompt_hint": "先输出素材清单、尺寸/风格/用途，再决定是否调用生成能力。",
        "preferred_capabilities": ["asset.generate:v1"],
        "required_capabilities": [],
    },
    {
        "id": "coding.incremental_delivery:v1",
        "label": "增量开发与定点修补",
        "category": "engineering",
        "description": "指导编码智能体优先做最小必要修改，保留健康代码并收敛重复定义。",
        "recommended_stage_types": ["coding"],
        "default_stage_assignment": True,
        "planner_visible": True,
        "prompt_hint": "优先定点修改、对齐文件清单和接口契约，避免整文件重写。",
        "preferred_capabilities": [],
        "required_capabilities": [],
    },
    {
        "id": "testing.closed_loop:v1",
        "label": "测试闭环策略",
        "category": "quality",
        "description": "指导测试智能体区分冒烟与全面测试，并把失败结论结构化打回开发环节。",
        "recommended_stage_types": ["testing"],
        "default_stage_assignment": True,
        "planner_visible": True,
        "prompt_hint": "先给出失败证据和复现路径，再决定回退到编码、素材或人工决策。",
        "preferred_capabilities": [],
        "required_capabilities": [],
    },
    {
        "id": "docs.delivery_authoring:v1",
        "label": "交付文档编写",
        "category": "delivery",
        "description": "帮助文档智能体整合实现结果、测试证据和运行方式，生成可交付文档。",
        "recommended_stage_types": ["docs"],
        "default_stage_assignment": True,
        "planner_visible": True,
        "prompt_hint": "整合运行说明、配置、限制、测试结论；若需要正式文档格式，可调用文档能力。",
        "preferred_capabilities": ["doc.write:v1"],
        "required_capabilities": [],
    },
]

DEFAULT_SKILL_SETTINGS: Dict[str, Any] = {
    "notes": "Skill 负责增强智能体的方法论与调用策略；Capability 负责实际执行。",
    "catalog": DEFAULT_SKILL_CATALOG,
}


def _normalize_skill_entry(entry: Dict[str, Any]) -> Dict[str, Any] | None:
    skill_id = str(entry.get("id") or "").strip()
    if not skill_id:
        return None
    recommended_stage_types = entry.get("recommended_stage_types")
    if not isinstance(recommended_stage_types, list):
        recommended_stage_types = []
    preferred_capabilities = entry.get("preferred_capabilities")
    if not isinstance(preferred_capabilities, list):
        preferred_capabilities = []
    required_capabilities = entry.get("required_capabilities")
    if not isinstance(required_capabilities, list):
        required_capabilities = []
    return {
        "id": skill_id,
        "label": str(entry.get("label") or skill_id).strip() or skill_id,
        "category": str(entry.get("category") or "general").strip() or "general",
        "description": str(entry.get("description") or "").strip(),
        "enabled": bool(entry.get("enabled", True)),
        "planner_visible": bool(entry.get("planner_visible", True)),
        "default_stage_assignment": bool(entry.get("default_stage_assignment", False)),
        "recommended_stage_types": [str(item).strip() for item in recommended_stage_types if str(item).strip()],
        "prompt_hint": str(entry.get("prompt_hint") or "").strip(),
        "preferred_capabilities": [str(item).strip() for item in preferred_capabilities if str(item).strip()],
        "required_capabilities": [str(item).strip() for item in required_capabilities if str(item).strip()],
    }


def merge_skill_settings(raw: Dict[str, Any] | None = None) -> Dict[str, Any]:
    merged = dict(DEFAULT_SKILL_SETTINGS)
    source = raw if isinstance(raw, dict) else {}
    merged["notes"] = str(source.get("notes") or merged.get("notes") or "")

    catalog: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    ordered_ids: List[str] = []
    for item in DEFAULT_SKILL_CATALOG:
        normalized = _normalize_skill_entry(item)
        if not normalized:
            continue
        by_id[normalized["id"]] = dict(normalized)
        ordered_ids.append(normalized["id"])
    if isinstance(source.get("catalog"), list):
        for item in source.get("catalog") or []:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_skill_entry(item)
            if not normalized:
                continue
            if normalized["id"] not in ordered_ids:
                ordered_ids.append(normalized["id"])
            by_id[normalized["id"]] = normalized
    for skill_id in ordered_ids:
        if skill_id in by_id:
            catalog.append(by_id[skill_id])
    merged["catalog"] = catalog
    return merged


def get_skill_catalog(raw: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    settings = merge_skill_settings(raw)
    return [dict(item) for item in settings.get("catalog") or [] if isinstance(item, dict)]


def get_default_skill_catalog() -> List[Dict[str, Any]]:
    return [dict(item) for item in merge_skill_settings().get("catalog") or [] if isinstance(item, dict)]


def get_skill_index(raw: Dict[str, Any] | None = None) -> Dict[str, Dict[str, Any]]:
    return {item["id"]: item for item in get_skill_catalog(raw)}


def default_skills_for_stage(stage_type: str, raw: Dict[str, Any] | None = None) -> List[str]:
    normalized = str(stage_type or "").strip()
    skills: List[str] = []
    for item in get_skill_catalog(raw):
        if not item.get("enabled", True):
            continue
        if not item.get("default_stage_assignment"):
            continue
        if normalized not in (item.get("recommended_stage_types") or []):
            continue
        skills.append(item["id"])
    return skills


def skill_prompt_view(raw: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    view: List[Dict[str, Any]] = []
    for item in get_skill_catalog(raw):
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
                "recommended_stage_types": item.get("recommended_stage_types") or [],
                "default_stage_assignment": bool(item.get("default_stage_assignment", False)),
                "prompt_hint": item.get("prompt_hint") or "",
                "preferred_capabilities": item.get("preferred_capabilities") or [],
                "required_capabilities": item.get("required_capabilities") or [],
            }
        )
    return view


def build_skill_runtime_context(skill_ids: List[str] | None, raw: Dict[str, Any] | None = None) -> str:
    if not isinstance(skill_ids, list) or not skill_ids:
        return ""
    index = get_skill_index(raw)
    rows: List[str] = []
    seen = set()
    for skill_id in skill_ids:
        normalized_id = str(skill_id or "").strip()
        if not normalized_id or normalized_id in seen:
            continue
        seen.add(normalized_id)
        skill = index.get(normalized_id)
        if not isinstance(skill, dict) or skill.get("enabled", True) is False:
            continue
        parts = [f"{skill.get('label') or normalized_id}（{normalized_id}）"]
        prompt_hint = str(skill.get("prompt_hint") or "").strip()
        if prompt_hint:
            parts.append(prompt_hint)
        preferred = [str(item).strip() for item in (skill.get("preferred_capabilities") or []) if str(item).strip()]
        required = [str(item).strip() for item in (skill.get("required_capabilities") or []) if str(item).strip()]
        if preferred:
            parts.append(f"优先考虑能力：{', '.join(preferred)}")
        if required:
            parts.append(f"若启用该 skill，必须确保可用能力：{', '.join(required)}")
        rows.append("- " + "；".join(parts))
    if not rows:
        return ""
    return (
        "[Skill Guidance]\n"
        "以下 skill 用于增强你的思考与执行策略；你可以直接调用 capability，skill 不是 capability 的唯一入口。\n"
        + "\n".join(rows)
    )


__all__ = [
    "DEFAULT_SKILL_CATALOG",
    "DEFAULT_SKILL_SETTINGS",
    "build_skill_runtime_context",
    "default_skills_for_stage",
    "get_default_skill_catalog",
    "get_skill_catalog",
    "get_skill_index",
    "merge_skill_settings",
    "skill_prompt_view",
]
