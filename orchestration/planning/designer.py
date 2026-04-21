"""动态工作流设计器，负责根据任务需求生成 Leader 规划结果。"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List

from core import Task
from orchestration.capabilities.registry import capability_prompt_view
from orchestration.skills.registry import skill_prompt_view
from orchestration.planning.stage_catalog import (
    REFERENCE_FLOW_PRESETS,
    build_stage_type_blueprints,
    normalize_execution_profile,
    normalize_stage_semantics,
    normalize_stage_type,
)
from orchestration.planning.workflow_plan import (
    _build_fallback_plan,
    _make_stage_instance,
    _normalize_stage_plan,
    resolve_conversation_groups,
    write_leader_plan_snapshot,
)


def _extract_json_block(text: str) -> Dict[str, Any] | None:
    if not text:
        return None
    raw = text.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw)
    if not match:
        return None
    try:
        obj = json.loads(match.group(1))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


class WorkflowDesigner:
    """根据任务规格、技能目录和能力目录生成动态阶段规划。"""

    def __init__(self, *, base_dir: str, select_model: Callable[[Task, str | None, List[str] | None], Any]):
        self.base_dir = base_dir
        self.select_model = select_model

    def plan(
        self,
        *,
        task: Task,
        template: Dict[str, Any],
        capability_settings: Dict[str, Any],
        skill_settings: Dict[str, Any],
    ) -> Dict[str, Any]:
        planner_model = self.select_model(task, "planning", [])
        spec = str((task.context or {}).get("spec") or "")
        stage_blueprints = build_stage_type_blueprints(capability_settings, skill_settings)
        reference_stages = []
        for item in template.get("stages", []) or []:
            if not isinstance(item, dict):
                continue
            normalized = _make_stage_instance(
                item.get("stage_type") or item.get("name") or "requirements",
                spec,
                name=item.get("name"),
                label=item.get("label"),
                role=item.get("role"),
                skills=item.get("skills"),
                capabilities=item.get("capabilities"),
                prompt_template=item.get("prompt_template"),
                acceptance_criteria=item.get("acceptance_criteria"),
                human_checkpoint=item.get("human_checkpoint"),
                capability_settings=capability_settings,
                skill_settings=skill_settings,
            )
            reference_stages.append({
                "name": normalized.get("name"),
                "stage_type": normalized.get("stage_type"),
                "execution_profile": normalized.get("execution_profile"),
                "stage_semantics": normalized.get("stage_semantics"),
                "label": normalized.get("label"),
                "role": normalized.get("role"),
                "skills": normalized.get("skills"),
                "capabilities": normalized.get("capabilities"),
            })

        fallback_plan = _build_fallback_plan(spec, capability_settings=capability_settings, skill_settings=skill_settings)
        preset_view = {
            name: [
                {
                    "name": item.get("name"),
                    "stage_type": normalize_stage_type(item.get("stage_type") or item.get("name")),
                    "execution_profile": normalize_execution_profile(item.get("execution_profile") or item.get("stage_type") or item.get("name")),
                    "stage_semantics": normalize_stage_semantics(item.get("stage_semantics"), item.get("stage_type") or item.get("execution_profile")),
                    "label": item.get("label"),
                    "role": item.get("role"),
                }
                for item in items
            ]
            for name, items in REFERENCE_FLOW_PRESETS.items()
        }
        capability_view = capability_prompt_view(capability_settings)
        skill_view = skill_prompt_view(skill_settings)
        blueprint_view = {
            stage_type: {
                "label": blueprint.get("label"),
                "role": blueprint.get("role"),
                "stage_semantics": blueprint.get("stage_semantics"),
                "execution_profile": blueprint.get("execution_profile"),
                "skills": blueprint.get("skills"),
                "capabilities": blueprint.get("capabilities"),
                "default_prompt_template": blueprint.get("prompt_template"),
                "default_acceptance_criteria": blueprint.get("acceptance_criteria"),
            }
            for stage_type, blueprint in stage_blueprints.items()
        }
        plan_prompt = (
            "你是项目的管理者/智者智能体，负责根据任务复杂度现场设计执行流程。\n"
            "你的职责不是套固定模板，而是根据任务目标、风险、规模，决定需要哪些角色、哪些阶段实例、阶段顺序、阶段数量以及每个阶段的提示词。\n"
            f"任务需求：{spec}\n"
            f"参考预设（只作参考，不能机械照搬）：{json.dumps(preset_view, ensure_ascii=False)}\n"
            f"Skill 目录（用于增强 agent 的方法论与调用策略）：{json.dumps(skill_view, ensure_ascii=False)}\n"
            f"能力目录（优先从这里选择 capabilities，不要凭空创造能力 ID）：{json.dumps(capability_view, ensure_ascii=False)}\n"
            f"参考执行轮廓蓝图（execution_profile 必须从这里选择或映射）：{json.dumps(blueprint_view, ensure_ascii=False)}\n"
            f"现有标准流程仅供参考：{json.dumps(reference_stages, ensure_ascii=False)}\n"
            "设计要求：\n"
            "1) 你可以自定义阶段实例的 name/label/role，也可以重复同一种 execution_profile 形成闭环；\n"
            "2) 阶段实例名称不固定，但 execution_profile 必须可落到执行器，只能是 requirements / architecture / assets / coding / testing / docs 之一；\n"
            "3) stage_semantics 用于表达业务语义，优先从 analysis / planning / design / creation / transformation / verification / delivery / decision / coordination 中选择；\n"
            "4) 简单任务要避免过度拆解，复杂任务可以拆出多个 creation / verification 闭环；\n"
            "5) 只要存在 coding execution_profile，前面必须至少有一个 requirements 和一个 architecture execution_profile；\n"
            "6) 若任务明显需要角色、图标、插画、游戏对象或图片素材，请在 coding 之前加入 assets execution_profile；\n"
            "7) 只要存在 testing execution_profile，前面必须至少有一个 coding execution_profile；\n"
            "8) skills 字段优先从 skill 目录中选择，用于增强该阶段 agent 的思考方式、约束和调用策略；基础阶段也应尽量显式给出合适的 skills；\n"
            "9) capabilities 字段优先从能力目录中选择，可为一个阶段组合多个能力；除非该阶段只是走最基础默认执行，否则不要留空；\n"
            "10) skill 不等于 capability：skill 负责方法论与调用策略，capability 负责实际执行；agent 可以直接调用 capability，不要求必须经由 skill；\n"
            "11) 如果某个 skill 依赖或强烈偏好特定 capability，请确保该阶段 capabilities 中包含对应能力；\n"
            "12) 如果某阶段需要主动调用特殊能力（如 asset.generate / doc.read / doc.write），必须把对应 capability 显式写进该阶段的 capabilities；\n"
            "13) 选择 capability 时要参考 input_fields / output_fields / supported_binding_types，确保它和该阶段的职责匹配；\n"
            "14) prompt_template 必须可以直接喂给对应执行智能体；如阶段可能主动调用能力，请在 prompt_template 中明确说明可调用目标；\n"
            "15) 如存在跨阶段协作，请输出 conversation_groups，或在阶段上附 conversation_group；\n"
            "16) 只输出严格 JSON，不要解释。\n"
            "输出格式："
            "{\"complexity\":\"simple|standard|complex\",\"reference_preset\":\"lightweight|standard|deep|custom\",\"summary\":\"...\",\"conversation_groups\":[{\"key\":\"dev_loop\",\"label\":\"开发闭环\",\"kind\":\"loop\",\"stages\":[\"core_impl\",\"qa_verification\"]}],\"stages\":[{\"name\":\"clarify_scope\",\"stage_semantics\":\"analysis\",\"execution_profile\":\"requirements\",\"stage_type\":\"requirements\",\"label\":\"范围澄清\",\"role\":\"产品分析师\",\"skills\":[\"requirements.discovery:v1\"],\"prompt_template\":\"...\",\"capabilities\":[],\"acceptance_criteria\":\"...\",\"depends_on\":[],\"human_checkpoint\":false},{\"name\":\"visual_assets\",\"stage_semantics\":\"creation\",\"execution_profile\":\"assets\",\"stage_type\":\"assets\",\"label\":\"视觉素材\",\"role\":\"视觉素材设计师\",\"skills\":[\"asset.prompting:v1\"],\"prompt_template\":\"...\",\"capabilities\":[\"asset.generate:v1\"],\"acceptance_criteria\":\"...\",\"depends_on\":[\"clarify_scope\"],\"human_checkpoint\":false},{\"name\":\"core_impl\",\"stage_semantics\":\"creation\",\"execution_profile\":\"coding\",\"stage_type\":\"coding\",\"label\":\"核心实现\",\"role\":\"软件工程师\",\"skills\":[\"coding.incremental_delivery:v1\"],\"conversation_group\":{\"key\":\"dev_loop\",\"label\":\"开发闭环\"},\"prompt_template\":\"...\",\"capabilities\":[],\"acceptance_criteria\":\"...\",\"depends_on\":[\"visual_assets\"],\"human_checkpoint\":false}]}"
        )

        try:
            out = planner_model.generate(plan_prompt, context=task.context)
            parsed = _extract_json_block(out)
        except Exception as exc:
            out = f"[planning error] {exc}"
            parsed = None
        if not isinstance(parsed, dict):
            parsed = fallback_plan
            used_fallback = True
        else:
            used_fallback = False

        planned_stages = _normalize_stage_plan(
            parsed.get("stages"),
            spec,
            capability_settings=capability_settings,
            skill_settings=skill_settings,
        )
        event_configs: Dict[str, Dict[str, Any]] = {}
        for stage in planned_stages:
            stage_name = stage["name"]
            stage_cfg = {
                "stage_type": stage["stage_type"],
                "execution_profile": stage["execution_profile"],
                "stage_semantics": stage["stage_semantics"],
                "label": stage.get("label"),
                "model_provider": task.context.get("default_model_provider"),
            }
            if stage.get("prompt_template"):
                stage_cfg["prompt_template"] = stage["prompt_template"]
            if stage.get("role"):
                stage_cfg["planned_role"] = stage["role"]
            if stage.get("skills"):
                stage_cfg["planned_skills"] = list(stage["skills"])
            if stage.get("acceptance_criteria"):
                stage_cfg["acceptance_criteria"] = stage["acceptance_criteria"]
            event_configs[stage_name] = stage_cfg

        conversation_groups = resolve_conversation_groups(planned_stages, parsed.get("conversation_groups"))
        task.context["event_configs"] = event_configs
        task.context["leader_plan"] = {
            "complexity": str(parsed.get("complexity") or fallback_plan.get("complexity") or "standard"),
            "reference_preset": str(parsed.get("reference_preset") or fallback_plan.get("reference_preset") or "custom"),
            "summary": str(parsed.get("summary") or fallback_plan.get("summary") or ""),
            "stages": planned_stages,
            "conversation_groups": conversation_groups,
            "raw_output": out,
            "used_fallback": used_fallback,
        }
        write_leader_plan_snapshot(task, self.base_dir, task.context["leader_plan"])
        return {
            "complexity": task.context["leader_plan"].get("complexity"),
            "reference_preset": task.context["leader_plan"].get("reference_preset"),
            "summary": task.context["leader_plan"].get("summary"),
            "conversation_groups": task.context["leader_plan"].get("conversation_groups"),
            "stages": planned_stages,
        }
