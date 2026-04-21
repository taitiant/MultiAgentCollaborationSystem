"""Leader 规划结果的归一化处理与回退工作流规划辅助函数。"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from core import Task
from domains.software_dev.agents.asset_agent import needs_visual_assets
from orchestration.planning.stage_catalog import (
    ARCHITECTURE_FILE_LIST_HINT,
    DEFAULT_ACCEPTANCE_CRITERIA,
    REFERENCE_FLOW_PRESETS,
    STAGE_EXECUTOR_TYPES,
    default_execution_profile_for_semantics,
    normalize_stage_type,
    normalize_stage_semantics,
    render_stage_prompt,
    resolve_stage_execution_profile,
    resolve_stage_semantics,
    stage_blueprint,
)


def _normalize_conversation_group(value: Any) -> Dict[str, Any] | None:
    if not value:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        return {"key": raw, "label": raw}
    if not isinstance(value, dict):
        return None
    raw_key = value.get("key") or value.get("id") or value.get("name")
    if not raw_key:
        return None
    key = str(raw_key).strip()
    if not key:
        return None
    normalized = {
        "key": key,
        "label": str(value.get("label") or value.get("title") or key).strip() or key,
    }
    kind = str(value.get("kind") or value.get("type") or "").strip()
    if kind:
        normalized["kind"] = kind
    return normalized


def _slugify_stage_name(value: str | None, fallback: str) -> str:
    raw = str(value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    return slug or fallback


def _estimate_task_complexity(spec: str) -> str:
    raw = str(spec or "")
    text = raw.lower()
    simple_hits = sum(1 for token in ["简单", "demo", "小游戏", "单页", "脚本", "样例", "mvp", "原型"] if token in raw or token in text)
    complex_hits = sum(1 for token in ["系统", "平台", "工作流", "多角色", "数据库", "权限", "部署", "接口", "后台", "前端", "协同", "langgraph", "agent", "多智能体"] if token in raw or token in text)
    if len(raw) > 220:
        complex_hits += 1
    if len(raw) < 80:
        simple_hits += 1
    if complex_hits >= 3:
        return "complex"
    if simple_hits >= 2 and complex_hits == 0:
        return "simple"
    return "standard"


def _make_stage_instance(
    stage_type: str,
    spec: str,
    name: str | None = None,
    used_names: set[str] | None = None,
    capability_settings: Dict[str, Any] | None = None,
    skill_settings: Dict[str, Any] | None = None,
    **overrides: Any,
) -> Dict[str, Any]:
    requested_semantics = overrides.get("stage_semantics") or overrides.get("semantic_type") or overrides.get("semantic")
    execution_profile = resolve_stage_execution_profile(
        {
            "execution_profile": overrides.get("execution_profile"),
            "stage_type": stage_type,
            "stage_semantics": requested_semantics,
            "name": name or overrides.get("name"),
            "label": overrides.get("label"),
        }
    )
    stage = stage_blueprint(execution_profile, capability_settings=capability_settings, skill_settings=skill_settings)
    fallback_name = stage["name"] if isinstance(stage.get("name"), str) else execution_profile
    base_name = _slugify_stage_name(name or overrides.get("name") or stage.get("label"), fallback=fallback_name)
    used = used_names if used_names is not None else set()
    unique_name = base_name
    suffix = 2
    while unique_name in used:
        unique_name = f"{base_name}_{suffix}"
        suffix += 1
    used.add(unique_name)
    stage_name = unique_name
    stage["name"] = stage_name
    stage["stage_type"] = execution_profile
    stage["execution_profile"] = execution_profile
    stage["stage_semantics"] = resolve_stage_semantics(
        {
            "stage_semantics": requested_semantics,
            "label": overrides.get("label") or stage.get("label") or stage_name,
            "name": stage_name,
            "stage_type": execution_profile,
        },
        execution_profile=execution_profile,
    )
    stage["label"] = str(overrides.get("label") or stage.get("label") or stage_name)
    stage["role"] = str(overrides.get("role") or stage.get("role") or f"{stage['stage_semantics']}-agent")
    prompt_override = overrides.get("prompt_template")
    stage["prompt_template"] = render_stage_prompt(
        stage_name,
        spec,
        prompt_override if isinstance(prompt_override, str) else None,
        stage_type=stage["stage_type"],
        execution_profile=execution_profile,
    )
    stage["acceptance_criteria"] = str(overrides.get("acceptance_criteria") or stage.get("acceptance_criteria") or DEFAULT_ACCEPTANCE_CRITERIA.get(execution_profile, ""))
    skills = overrides.get("skills")
    stage["skills"] = list(skills) if isinstance(skills, list) and skills else list(stage.get("skills") or [])
    caps = overrides.get("capabilities")
    stage["capabilities"] = list(caps) if isinstance(caps, list) and caps else list(stage.get("capabilities") or [])
    stage["human_checkpoint"] = bool(overrides.get("human_checkpoint", stage.get("human_checkpoint", False)))
    depends_on = overrides.get("depends_on")
    stage["depends_on"] = [str(dep) for dep in depends_on if dep] if isinstance(depends_on, list) else []
    conversation_group = _normalize_conversation_group(
        overrides.get("conversation_group")
        or overrides.get("group_key")
        or overrides.get("group")
        or overrides.get("loop_group")
        or overrides.get("collaboration_group")
    )
    if conversation_group:
        stage["conversation_group"] = conversation_group
    if execution_profile == "architecture" and "文件清单" not in str(stage.get("prompt_template") or ""):
        stage["prompt_template"] = (str(stage["prompt_template"]).strip() + ARCHITECTURE_FILE_LIST_HINT).strip()
    return stage


def _ensure_stage_prerequisites(
    stages: List[Dict[str, Any]],
    spec: str,
    capability_settings: Dict[str, Any] | None = None,
    skill_settings: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    result = [dict(stage) for stage in stages if isinstance(stage, dict) and stage.get("name")]
    used_names = {str(stage.get("name")) for stage in result if stage.get("name")}
    visual_assets_required = needs_visual_assets(spec)

    def has_prior(before_index: int, stage_type: str) -> bool:
        normalized = resolve_stage_execution_profile(stage_type)
        return any(resolve_stage_execution_profile(stage) == normalized for stage in result[:before_index])

    def insert_before(before_index: int, stage_type: str, label: str | None = None) -> int:
        if has_prior(before_index, stage_type):
            return before_index
        auto_stage = _make_stage_instance(
            stage_type,
            spec,
            used_names=used_names,
            label=label,
            capability_settings=capability_settings,
            skill_settings=skill_settings,
        )
        result.insert(before_index, auto_stage)
        return before_index + 1

    idx = 0
    while idx < len(result):
        execution_profile = resolve_stage_execution_profile(result[idx])
        if execution_profile == "architecture":
            idx = insert_before(idx, "requirements", label="自动补全需求")
        elif execution_profile == "assets":
            idx = insert_before(idx, "requirements", label="自动补全需求")
            idx = insert_before(idx, "architecture", label="自动补全方案")
        elif execution_profile == "coding":
            idx = insert_before(idx, "requirements", label="自动补全需求")
            idx = insert_before(idx, "architecture", label="自动补全方案")
            if visual_assets_required:
                idx = insert_before(idx, "assets", label="自动补全素材")
        elif execution_profile == "testing":
            idx = insert_before(idx, "requirements", label="自动补全需求")
            idx = insert_before(idx, "architecture", label="自动补全方案")
            if visual_assets_required:
                idx = insert_before(idx, "assets", label="自动补全素材")
            idx = insert_before(idx, "coding", label="自动补全实现")
        elif execution_profile == "docs" and any(resolve_stage_execution_profile(item) == "coding" for item in result):
            idx = insert_before(idx, "requirements", label="自动补全需求")
            idx = insert_before(idx, "architecture", label="自动补全方案")
            if visual_assets_required:
                idx = insert_before(idx, "assets", label="自动补全素材")
            idx = insert_before(idx, "coding", label="自动补全实现")
        idx += 1

    all_names = {str(stage.get("name")) for stage in result if stage.get("name")}
    for i, stage in enumerate(result):
        raw_deps = stage.get("depends_on") if isinstance(stage.get("depends_on"), list) else []
        deps = [str(dep) for dep in raw_deps if dep in all_names and dep != stage.get("name")]
        stage["depends_on"] = deps or ([str(result[i - 1].get("name"))] if i > 0 else [])
        execution_profile = resolve_stage_execution_profile(stage)
        stage["stage_type"] = execution_profile
        stage["execution_profile"] = execution_profile
        stage["stage_semantics"] = resolve_stage_semantics(stage, execution_profile=execution_profile)
    return result


def _normalize_stage_plan(
    raw_stages: List[Dict[str, Any]],
    spec: str,
    capability_settings: Dict[str, Any] | None = None,
    skill_settings: Dict[str, Any] | None = None,
    enforce_prerequisites: bool = False,
) -> List[Dict[str, Any]]:
    planned: List[Dict[str, Any]] = []
    used_names: set[str] = set()
    for idx, item in enumerate(raw_stages or [], start=1):
        if not isinstance(item, dict):
            continue
        requested_semantics = item.get("stage_semantics") or item.get("semantic_type") or item.get("semantic")
        execution_profile = resolve_stage_execution_profile(
            {
                "execution_profile": item.get("execution_profile"),
                "stage_type": item.get("stage_type") or item.get("executor_type"),
                "stage_semantics": requested_semantics,
                "name": item.get("name"),
                "label": item.get("label"),
            },
            fallback="",
        )
        if not execution_profile and requested_semantics:
            execution_profile = default_execution_profile_for_semantics(str(requested_semantics))
        if execution_profile not in STAGE_EXECUTOR_TYPES:
            continue
        stage = _make_stage_instance(
            execution_profile,
            spec,
            name=item.get("name") or item.get("id") or item.get("label") or f"{execution_profile}_{idx}",
            used_names=used_names,
            label=item.get("label"),
            role=item.get("role"),
            stage_semantics=requested_semantics,
            skills=item.get("skills"),
            prompt_template=item.get("prompt_template"),
            capabilities=item.get("capabilities"),
            acceptance_criteria=item.get("acceptance_criteria"),
            human_checkpoint=item.get("human_checkpoint"),
            depends_on=item.get("depends_on"),
            conversation_group=item.get("conversation_group") or item.get("group_key") or item.get("group") or item.get("loop_group") or item.get("collaboration_group"),
            capability_settings=capability_settings,
            skill_settings=skill_settings,
        )
        planned.append(stage)
    if not planned:
        return []
    if enforce_prerequisites:
        return _ensure_stage_prerequisites(
            planned,
            spec,
            capability_settings=capability_settings,
            skill_settings=skill_settings,
        )
    all_names = {str(stage.get("name")) for stage in planned if stage.get("name")}
    for index, stage in enumerate(planned):
        raw_deps = stage.get("depends_on") if isinstance(stage.get("depends_on"), list) else []
        deps = [str(dep) for dep in raw_deps if dep in all_names and dep != stage.get("name")]
        stage["depends_on"] = deps or ([str(planned[index - 1].get("name"))] if index > 0 else [])
        execution_profile = resolve_stage_execution_profile(stage)
        stage["stage_type"] = execution_profile
        stage["execution_profile"] = execution_profile
        stage["stage_semantics"] = resolve_stage_semantics(stage, execution_profile=execution_profile)
    return planned


def resolve_conversation_groups(stages: List[Dict[str, Any]], raw_groups: Any = None) -> List[Dict[str, Any]]:
    normalized_stages = [stage for stage in (stages or []) if isinstance(stage, dict) and stage.get("name")]
    stage_names = [str(stage.get("name")) for stage in normalized_stages if stage.get("name")]
    stage_map = {str(stage.get("name")): stage for stage in normalized_stages if stage.get("name")}
    groups: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    grouped_stage_names: set[str] = set()

    def add_group(key: str, label: str, members: List[str], kind: str = "stage_flow") -> None:
        clean_members = [name for name in members if name in stage_map]
        if not key or not clean_members or key in seen_keys:
            return
        seen_keys.add(key)
        groups.append({"key": key, "label": label or key, "kind": kind or "stage_flow", "stage_names": clean_members})
        grouped_stage_names.update(clean_members)

    if isinstance(raw_groups, list):
        for item in raw_groups:
            normalized = _normalize_conversation_group(item)
            if not normalized or not isinstance(item, dict):
                continue
            raw_members = item.get("stage_names") or item.get("stages") or item.get("members") or []
            members = [str(name) for name in raw_members if str(name) in stage_map] if isinstance(raw_members, list) else []
            add_group(normalized["key"], normalized["label"], members, str(normalized.get("kind") or "stage_flow"))

    for stage in normalized_stages:
        group_cfg = _normalize_conversation_group(
            stage.get("conversation_group")
            or stage.get("group_key")
            or stage.get("group")
            or stage.get("loop_group")
            or stage.get("collaboration_group")
        )
        if not group_cfg:
            continue
        existing = next((group for group in groups if group["key"] == group_cfg["key"]), None)
        if existing:
            if stage["name"] not in existing["stage_names"]:
                existing["stage_names"].append(stage["name"])
                grouped_stage_names.add(stage["name"])
            continue
        add_group(group_cfg["key"], group_cfg["label"], [stage["name"]], str(group_cfg.get("kind") or "stage_flow"))

    current_chain: List[str] = []
    for stage in normalized_stages:
        stage_name = str(stage["name"])
        if stage_name in grouped_stage_names:
            if len(current_chain) > 1:
                add_group(f"flow:{current_chain[0]}:{current_chain[-1]}", "开发闭环", current_chain[:], "loop")
            current_chain = []
            continue
        execution_profile = resolve_stage_execution_profile(stage)
        if execution_profile in {"coding", "testing"}:
            current_chain.append(stage_name)
        else:
            if len(current_chain) > 1:
                add_group(f"flow:{current_chain[0]}:{current_chain[-1]}", "开发闭环", current_chain[:], "loop")
            current_chain = []
    if len(current_chain) > 1:
        add_group(f"flow:{current_chain[0]}:{current_chain[-1]}", "开发闭环", current_chain[:], "loop")

    for stage_name in stage_names:
        if stage_name in grouped_stage_names:
            continue
        stage = stage_map[stage_name]
        add_group(stage_name, str(stage.get("label") or stage_name), [stage_name], "stage")

    for stage in normalized_stages:
        stage_name = str(stage["name"])
        matched = next((group for group in groups if stage_name in group["stage_names"]), None)
        if matched:
            stage["conversation_group"] = {"key": matched["key"], "label": matched["label"], "kind": matched.get("kind") or "stage_flow"}
    return groups


def write_leader_plan_snapshot(task: Task, base_dir: str, leader_plan: Dict[str, Any]) -> str:
    workspace = os.path.abspath(task.workspace_path or os.path.join(base_dir, task.task_id))
    plan_dir = os.path.join(workspace, "plan")
    os.makedirs(plan_dir, exist_ok=True)
    plan_path = os.path.join(plan_dir, "leader_plan.json")
    with open(plan_path, "w", encoding="utf-8") as handle:
        json.dump(leader_plan or {}, handle, ensure_ascii=False, indent=2)
    return plan_path


def _build_fallback_plan(
    spec: str,
    capability_settings: Dict[str, Any] | None = None,
    skill_settings: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    complexity = _estimate_task_complexity(spec)
    preset_name = "lightweight" if complexity == "simple" else "deep" if complexity == "complex" else "standard"
    stages = _normalize_stage_plan(
        REFERENCE_FLOW_PRESETS.get(preset_name, []),
        spec,
        capability_settings=capability_settings,
        skill_settings=skill_settings,
        enforce_prerequisites=True,
    )
    visual_assets_required = needs_visual_assets(spec)
    if visual_assets_required:
        stages = _ensure_stage_prerequisites(
            stages,
            spec,
            capability_settings=capability_settings,
            skill_settings=skill_settings,
        )
    summary_map = {
        "simple": "任务偏简单，采用精简流程，避免过度设计。",
        "standard": "任务复杂度中等，采用需求→方案→实现→测试→交付的标准闭环。",
        "complex": "任务复杂度较高，采用更稳健的多阶段研发流程。",
    }
    summary = summary_map.get(complexity, "采用标准执行流程。")
    if visual_assets_required:
        summary += " 任务存在明确视觉素材需求，已自动补入素材规划/生成阶段。"
    return {"complexity": complexity, "reference_preset": preset_name, "summary": summary, "stages": stages}


__all__ = [
    "_build_fallback_plan",
    "_make_stage_instance",
    "_normalize_stage_plan",
    "resolve_conversation_groups",
    "write_leader_plan_snapshot",
]
