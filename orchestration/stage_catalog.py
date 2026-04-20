from __future__ import annotations

import re
from typing import Any, Dict

from orchestration.capability_registry import default_capabilities_for_stage
from orchestration.skill_registry import default_skills_for_stage


DEFAULT_STAGE_PROMPTS = {
    "requirements": (
        "你是分析智能体。基于用户需求输出严谨的分析文档。\n"
        "需求：{spec}\n"
        "输出 Markdown，必须包含：\n"
        "1) 目标与范围\n2) 约束与风险\n3) 验收标准\n4) 边界说明\n"
        "禁止越界输出实现代码。"
    ),
    "architecture": (
        "你是方案设计智能体。请基于已有需求和上下文输出可落地的方案文档。\n"
        "原始需求：{spec}\n"
        "输出 Markdown，必须包含：\n"
        "1) 方案思路\n2) 模块职责\n3) 接口/数据流\n"
        "4) 一个标题为“## 文件清单”的章节，按每行一个相对路径列出文件（可包含深层目录），例如 code/api/app.py。\n"
        "不要输出实现代码。"
    ),
    "assets": (
        "你是素材与视觉内容智能体。请基于需求和方案，规划并生成当前项目所需的视觉素材。\n"
        "需求：{spec}\n"
        "优先输出关键素材清单、可复用的生图提示词，以及可直接接入的 SVG 占位图。"
    ),
    "coding": (
        "你是实现智能体。请基于需求、方案和已有文件，为指定文件生成或修复内容。\n"
        "需求：{spec}\n"
        "必须遵循方案文档中的文件路径和职责，不得擅自改动路径。"
    ),
    "testing": (
        "你是验证智能体。请基于需求、方案和现有实现执行验证。\n"
        "需求：{spec}\n"
        "优先覆盖主流程、关键边界和最近改动带来的回归风险。"
    ),
    "docs": (
        "你是交付文档智能体。请基于当前产物与验证结果生成交付说明。\n"
        "需求：{spec}\n"
        "输出应包含运行方式、配置、输入输出示例、限制说明。"
    ),
}

DEFAULT_ACCEPTANCE_CRITERIA = {
    "requirements": "目标、边界、约束与验收标准明确，没有越界到实现细节。",
    "architecture": "方案可落地，并给出清晰可执行的文件清单或结构设计。",
    "assets": "明确关键视觉资源，生成可接入的占位素材，并保留清晰提示词。",
    "coding": "实现与需求和方案一致，生成代码可通过基础语法/冒烟校验。",
    "testing": "验证结果覆盖关键功能与主要风险，并明确失败原因或回退结论。",
    "docs": "交付文档说明清晰，覆盖运行方式、配置、限制与验证结果。",
}

ARCHITECTURE_FILE_LIST_HINT = (
    "\n\n输出要求补充：必须包含标题为“## 文件清单”的章节，"
    "并按每行一个相对路径列出待实现文件（例如 code/main.py、code/game/board.py）。"
)

TEXT_OUTPUT_QUALITY_GUARDRAIL = (
    "\n\n输出前自检：\n"
    "- 通读全文一遍，修正明显错别字、漏字、残句和重复标点。\n"
    "- 不要出现“少一个字”“半句话断掉”“重复两个标点”这类低级文本问题。\n"
    "- 保持标题、列表、路径和术语前后一致。"
)

STAGE_SEMANTIC_ALIASES = {
    "analysis": "analysis",
    "analyst": "analysis",
    "requirements": "analysis",
    "requirement": "analysis",
    "clarification": "analysis",
    "clarify": "analysis",
    "scope": "analysis",
    "discovery": "analysis",
    "需求": "analysis",
    "需求分析": "analysis",
    "需求澄清": "analysis",
    "planning": "planning",
    "plan": "planning",
    "规划": "planning",
    "design": "design",
    "architecture": "design",
    "arch": "design",
    "solution": "design",
    "solution_design": "design",
    "technical_design": "design",
    "方案": "design",
    "架构": "design",
    "creation": "creation",
    "create": "creation",
    "coding": "creation",
    "implementation": "creation",
    "implement": "creation",
    "develop": "creation",
    "development": "creation",
    "build": "creation",
    "assets": "creation",
    "asset": "creation",
    "visual": "creation",
    "media": "creation",
    "生成": "creation",
    "实现": "creation",
    "素材": "creation",
    "transformation": "transformation",
    "transform": "transformation",
    "refine": "transformation",
    "rewrite": "transformation",
    "conversion": "transformation",
    "转换": "transformation",
    "改写": "transformation",
    "verification": "verification",
    "verify": "verification",
    "validation": "verification",
    "testing": "verification",
    "test": "verification",
    "qa": "verification",
    "review": "verification",
    "验收": "verification",
    "测试": "verification",
    "验证": "verification",
    "delivery": "delivery",
    "deliver": "delivery",
    "docs": "delivery",
    "doc": "delivery",
    "documentation": "delivery",
    "readme": "delivery",
    "handoff": "delivery",
    "交付": "delivery",
    "文档": "delivery",
    "说明": "delivery",
    "decision": "decision",
    "decide": "decision",
    "approval": "decision",
    "gate": "decision",
    "decision_gate": "decision",
    "决策": "decision",
    "审批": "decision",
    "coordination": "coordination",
    "coordinate": "coordination",
    "sync": "coordination",
    "orchestrate": "coordination",
    "collaboration": "coordination",
    "协同": "coordination",
    "编排": "coordination",
}

EXECUTION_PROFILE_ALIASES = {
    "requirements": "requirements",
    "requirement": "requirements",
    "analysis": "requirements",
    "analyst": "requirements",
    "clarification": "requirements",
    "clarify": "requirements",
    "scope": "requirements",
    "discovery": "requirements",
    "planning": "requirements",
    "product": "requirements",
    "需求": "requirements",
    "需求分析": "requirements",
    "需求澄清": "requirements",
    "architecture": "architecture",
    "arch": "architecture",
    "design": "architecture",
    "solution": "architecture",
    "solution_design": "architecture",
    "technical_design": "architecture",
    "规划": "architecture",
    "方案": "architecture",
    "架构": "architecture",
    "assets": "assets",
    "asset": "assets",
    "asset_generation": "assets",
    "visual": "assets",
    "art": "assets",
    "media": "assets",
    "素材": "assets",
    "视觉": "assets",
    "美术": "assets",
    "配图": "assets",
    "图片": "assets",
    "coding": "coding",
    "code": "coding",
    "implementation": "coding",
    "implement": "coding",
    "develop": "coding",
    "development": "coding",
    "build": "coding",
    "patch": "coding",
    "fix": "coding",
    "bugfix": "coding",
    "repair": "coding",
    "creation": "coding",
    "transformation": "coding",
    "编码": "coding",
    "开发": "coding",
    "实现": "coding",
    "修复": "coding",
    "testing": "testing",
    "test": "testing",
    "qa": "testing",
    "verification": "testing",
    "verify": "testing",
    "validation": "testing",
    "review": "testing",
    "验收": "testing",
    "测试": "testing",
    "验证": "testing",
    "docs": "docs",
    "doc": "docs",
    "documentation": "docs",
    "readme": "docs",
    "handoff": "docs",
    "delivery": "docs",
    "交付": "docs",
    "文档": "docs",
    "说明": "docs",
}

STAGE_TYPE_ALIASES = EXECUTION_PROFILE_ALIASES

DEFAULT_STAGE_SEMANTICS_BY_PROFILE = {
    "requirements": "analysis",
    "architecture": "design",
    "assets": "creation",
    "coding": "creation",
    "testing": "verification",
    "docs": "delivery",
}

DEFAULT_EXECUTION_PROFILE_BY_SEMANTICS = {
    "analysis": "requirements",
    "planning": "architecture",
    "design": "architecture",
    "creation": "coding",
    "transformation": "coding",
    "verification": "testing",
    "delivery": "docs",
    "decision": "requirements",
    "coordination": "requirements",
}

DEFAULT_BLOCKING_REVIEW_STAGE_TYPES = {"requirements", "architecture", "assets", "docs"}

STAGE_EXECUTION_PROFILES = {"requirements", "architecture", "assets", "coding", "testing", "docs"}
STAGE_EXECUTOR_TYPES = STAGE_EXECUTION_PROFILES

STAGE_TYPE_PROFILES = {
    "requirements": {
        "name": "requirements",
        "label": "分析文本",
        "role": "分析智能体",
        "human_checkpoint": False,
        "default_stage_semantics": "analysis",
    },
    "architecture": {
        "name": "architecture",
        "label": "方案设计",
        "role": "方案智能体",
        "human_checkpoint": False,
        "default_stage_semantics": "design",
    },
    "assets": {
        "name": "assets",
        "label": "素材生成",
        "role": "素材智能体",
        "human_checkpoint": False,
        "default_stage_semantics": "creation",
    },
    "coding": {
        "name": "coding",
        "label": "内容实现",
        "role": "实现智能体",
        "human_checkpoint": False,
        "default_stage_semantics": "creation",
    },
    "testing": {
        "name": "testing",
        "label": "验证检查",
        "role": "验证智能体",
        "human_checkpoint": False,
        "default_stage_semantics": "verification",
    },
    "docs": {
        "name": "docs",
        "label": "交付整理",
        "role": "交付智能体",
        "human_checkpoint": False,
        "default_stage_semantics": "delivery",
    },
}

REFERENCE_FLOW_PRESETS = {
    "lightweight": [
        {"name": "clarify_scope", "stage_type": "requirements", "stage_semantics": "analysis", "label": "任务澄清", "role": "分析智能体"},
        {"name": "solution_design", "stage_type": "architecture", "stage_semantics": "design", "label": "实现方案", "role": "方案智能体"},
        {"name": "implementation", "stage_type": "coding", "stage_semantics": "creation", "label": "核心实现", "role": "实现智能体"},
        {"name": "verification", "stage_type": "testing", "stage_semantics": "verification", "label": "验证测试", "role": "验证智能体"},
    ],
    "standard": [
        {"name": "clarify_scope", "stage_type": "requirements", "stage_semantics": "analysis", "label": "需求分析", "role": "分析智能体"},
        {"name": "solution_design", "stage_type": "architecture", "stage_semantics": "design", "label": "技术方案", "role": "方案智能体"},
        {"name": "implementation", "stage_type": "coding", "stage_semantics": "creation", "label": "内容实现", "role": "实现智能体"},
        {"name": "verification", "stage_type": "testing", "stage_semantics": "verification", "label": "验证测试", "role": "验证智能体"},
        {"name": "delivery", "stage_type": "docs", "stage_semantics": "delivery", "label": "交付整理", "role": "交付智能体"},
    ],
    "deep": [
        {"name": "problem_analysis", "stage_type": "requirements", "stage_semantics": "analysis", "label": "问题分析", "role": "分析智能体"},
        {"name": "solution_architecture", "stage_type": "architecture", "stage_semantics": "design", "label": "方案设计", "role": "方案智能体"},
        {"name": "core_creation", "stage_type": "coding", "stage_semantics": "creation", "label": "核心产出", "role": "实现智能体"},
        {"name": "system_verification", "stage_type": "testing", "stage_semantics": "verification", "label": "系统验证", "role": "验证智能体"},
        {"name": "delivery_handoff", "stage_type": "docs", "stage_semantics": "delivery", "label": "交付说明", "role": "交付智能体"},
    ],
}


def _slug(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")


def normalize_stage_type(stage_type: str | None) -> str:
    raw = str(stage_type or "").strip().lower()
    if not raw:
        return "requirements"
    if raw in STAGE_TYPE_ALIASES:
        return STAGE_TYPE_ALIASES[raw]
    slug = _slug(raw)
    if slug in STAGE_TYPE_ALIASES:
        return STAGE_TYPE_ALIASES[slug]
    return slug or "requirements"


def normalize_execution_profile(execution_profile: str | None, fallback: str | None = "requirements") -> str:
    raw = str(execution_profile or "").strip()
    if not raw:
        return fallback or ""
    normalized = normalize_stage_type(execution_profile)
    if normalized in STAGE_EXECUTION_PROFILES:
        return normalized
    return fallback or ""


def normalize_stage_semantics(stage_semantics: str | None, execution_profile: str | None = None) -> str:
    raw = str(stage_semantics or "").strip().lower()
    if raw:
        if raw in STAGE_SEMANTIC_ALIASES:
            return STAGE_SEMANTIC_ALIASES[raw]
        slug = _slug(raw)
        if slug in STAGE_SEMANTIC_ALIASES:
            return STAGE_SEMANTIC_ALIASES[slug]
    if execution_profile:
        return default_stage_semantics_for_profile(execution_profile)
    return "analysis"


def default_stage_semantics_for_profile(execution_profile: str | None) -> str:
    normalized = normalize_execution_profile(execution_profile)
    return DEFAULT_STAGE_SEMANTICS_BY_PROFILE.get(normalized, "analysis")


def default_execution_profile_for_semantics(stage_semantics: str | None) -> str:
    normalized = normalize_stage_semantics(stage_semantics)
    return DEFAULT_EXECUTION_PROFILE_BY_SEMANTICS.get(normalized, "requirements")


def resolve_stage_execution_profile(stage: Dict[str, Any] | str | None, fallback: str = "requirements") -> str:
    candidates = []
    if isinstance(stage, dict):
        candidates = [
            stage.get("execution_profile"),
            stage.get("stage_type"),
            stage.get("executor_type"),
            stage.get("profile"),
            stage.get("name"),
            stage.get("label"),
        ]
    else:
        candidates = [stage]
    for candidate in candidates:
        normalized = normalize_execution_profile(str(candidate or "").strip(), fallback=None)
        if normalized:
            return normalized
    if isinstance(stage, dict):
        semantics = (
            stage.get("stage_semantics")
            or stage.get("semantic_type")
            or stage.get("semantic")
        )
        if semantics:
            return default_execution_profile_for_semantics(str(semantics))
    return fallback


def resolve_stage_semantics(stage: Dict[str, Any] | str | None, execution_profile: str | None = None) -> str:
    if isinstance(stage, dict):
        explicit = (
            stage.get("stage_semantics")
            or stage.get("semantic_type")
            or stage.get("semantic")
        )
        if explicit:
            return normalize_stage_semantics(str(explicit), execution_profile=execution_profile)
        for candidate in [stage.get("label"), stage.get("name"), stage.get("stage_type")]:
            normalized = normalize_stage_semantics(str(candidate or ""), execution_profile=None)
            if normalized != "analysis" or _slug(str(candidate or "")) in STAGE_SEMANTIC_ALIASES:
                return normalized
        return normalize_stage_semantics("", execution_profile=execution_profile)
    return normalize_stage_semantics(str(stage or ""), execution_profile=execution_profile)


def stage_prompt_key(
    stage_name: str,
    stage_type: str | None = None,
    execution_profile: str | None = None,
) -> str:
    profile = normalize_execution_profile(execution_profile or stage_type or stage_name)
    return profile if profile in DEFAULT_STAGE_PROMPTS else "requirements"


def render_stage_prompt(
    stage_name: str,
    spec: str,
    override_prompt: str | None = None,
    stage_type: str | None = None,
    execution_profile: str | None = None,
) -> str:
    prompt_key = stage_prompt_key(stage_name, stage_type=stage_type, execution_profile=execution_profile)
    template = override_prompt or DEFAULT_STAGE_PROMPTS.get(prompt_key, "{spec}")
    try:
        return template.format(spec=spec)
    except Exception:
        return template


def build_stage_type_blueprints(
    capability_settings: Dict[str, Any] | None = None,
    skill_settings: Dict[str, Any] | None = None,
) -> Dict[str, Dict[str, Any]]:
    blueprints: Dict[str, Dict[str, Any]] = {}
    for execution_profile, profile in STAGE_TYPE_PROFILES.items():
        normalized = normalize_execution_profile(execution_profile)
        stage_semantics = normalize_stage_semantics(profile.get("default_stage_semantics"), execution_profile=normalized)
        blueprints[normalized] = {
            "name": profile["name"],
            "stage_type": normalized,
            "execution_profile": normalized,
            "stage_semantics": stage_semantics,
            "label": profile["label"],
            "role": profile["role"],
            "skills": default_skills_for_stage(normalized, skill_settings),
            "capabilities": default_capabilities_for_stage(normalized, capability_settings),
            "prompt_template": DEFAULT_STAGE_PROMPTS[normalized],
            "acceptance_criteria": DEFAULT_ACCEPTANCE_CRITERIA[normalized],
            "human_checkpoint": bool(profile.get("human_checkpoint", False)),
        }
    return blueprints


STAGE_TYPE_BLUEPRINTS = build_stage_type_blueprints()


def stage_blueprint(
    stage_type: str,
    capability_settings: Dict[str, Any] | None = None,
    skill_settings: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    normalized = normalize_execution_profile(stage_type)
    blueprints = build_stage_type_blueprints(capability_settings, skill_settings) if (capability_settings or skill_settings) else STAGE_TYPE_BLUEPRINTS
    blueprint = dict(blueprints.get(normalized) or blueprints["requirements"])
    blueprint["stage_type"] = normalized
    blueprint["execution_profile"] = normalized
    blueprint["stage_semantics"] = normalize_stage_semantics(blueprint.get("stage_semantics"), execution_profile=normalized)
    return blueprint


__all__ = [
    "ARCHITECTURE_FILE_LIST_HINT",
    "DEFAULT_ACCEPTANCE_CRITERIA",
    "DEFAULT_BLOCKING_REVIEW_STAGE_TYPES",
    "DEFAULT_EXECUTION_PROFILE_BY_SEMANTICS",
    "DEFAULT_STAGE_PROMPTS",
    "DEFAULT_STAGE_SEMANTICS_BY_PROFILE",
    "EXECUTION_PROFILE_ALIASES",
    "REFERENCE_FLOW_PRESETS",
    "STAGE_EXECUTION_PROFILES",
    "STAGE_EXECUTOR_TYPES",
    "STAGE_SEMANTIC_ALIASES",
    "STAGE_TYPE_ALIASES",
    "STAGE_TYPE_BLUEPRINTS",
    "TEXT_OUTPUT_QUALITY_GUARDRAIL",
    "build_stage_type_blueprints",
    "default_execution_profile_for_semantics",
    "default_stage_semantics_for_profile",
    "normalize_execution_profile",
    "normalize_stage_semantics",
    "normalize_stage_type",
    "render_stage_prompt",
    "resolve_stage_execution_profile",
    "resolve_stage_semantics",
    "stage_blueprint",
    "stage_prompt_key",
]
