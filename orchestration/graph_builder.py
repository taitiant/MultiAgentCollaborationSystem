from __future__ import annotations
import os
import yaml
import db
import json
import re
import shutil
from typing import Dict, Any, List, Callable, Optional
from langgraph.graph import StateGraph
from langgraph.graph import END

from core import Task, SystemState, new_event
from adapters.model_registry import ModelRegistry
from domains.software_dev.agents.asset_agent import AssetAgent
from domains.software_dev.agents.document_agents import ArchAgent, DocAgent, ReqAgent
from domains.software_dev.agents.patch_agent import PatchAgent
from domains.software_dev.agents.test_agent import TestAgent
from orchestration.capability_invoker import (
    build_capability_invoke_prompt,
    build_requested_capability_execution,
    extract_capability_invocations,
)
from orchestration.capability_registry import capability_prompt_view, merge_capability_settings
from orchestration.capability_runtime import CapabilityRuntime
from orchestration.collaboration import CollaborationHub
from orchestration.document_rules import (
    _architecture_validation_issues,
    _docs_validation_issues,
    _extract_architecture_file_list,
    _extract_file_paths_from_lines,
    _infer_declared_stack,
    _infer_project_stack,
    _normalize_architecture_file_list,
    _normalize_architecture_markdown,
)
from orchestration.mcp_registry import merge_mcp_settings
from orchestration.file_utils import write_text, ensure_workspace
from orchestration.stage_catalog import (
    DEFAULT_BLOCKING_REVIEW_STAGE_TYPES,
    REFERENCE_FLOW_PRESETS,
    STAGE_EXECUTOR_TYPES,
    TEXT_OUTPUT_QUALITY_GUARDRAIL,
    build_stage_type_blueprints,
    normalize_execution_profile,
    normalize_stage_semantics,
    normalize_stage_type,
    resolve_stage_execution_profile,
    resolve_stage_semantics,
)
from orchestration.skill_registry import (
    build_skill_runtime_context,
    merge_skill_settings,
    skill_prompt_view,
)
from orchestration.workflow_plan import (
    _build_fallback_plan,
    _make_stage_instance,
    _normalize_stage_plan,
    resolve_conversation_groups,
    write_leader_plan_snapshot,
)
from orchestration.workspace_cleanup import cleanup_architecture_orphan_files
from core import AgentMessage
from storage.file_store import FileStore


def _emit_stage_progress(progress_callback: Optional[Callable[[Dict[str, Any]], None]], **payload: Any) -> None:
    if not progress_callback:
        return
    try:
        progress_callback(payload)
    except Exception:
        return


def _model_failure_text(value: Any) -> str:
    text = str(value or "")
    if not text.startswith("["):
        return ""
    lowered = text.lower()
    if "error" in lowered or "empty response" in lowered or "disabled" in lowered:
        return text
    return ""


def _build_rework_guidance(stage_type: str, feedback: str, attempt: int = 0) -> str:
    normalized_type = normalize_stage_type(stage_type)
    raw = str(feedback or "")
    compact = raw.lower()
    hints: List[str] = []
    if normalized_type == "coding":
        hints.append("只处理评审明确指出的问题，优先做最小必要修改，不要顺手扩写无关功能。")
        hints.append("必须与 architecture.md 的文件清单保持一致；若调用方引用了不存在的模块，优先修改调用方去对齐清单内已有文件，不要随意新增清单外路径。")
        hints.append("若多个文件出现重复定义，请保留单一事实来源，其余文件改为 import / 复用 / 委托，不要复制常量、规则表、状态模型。")
        if any(token in compact for token in ("重复", "并行", "两套", "一致性", "duplicate")):
            hints.append("本轮重点先消除结构重复与职责冲突，再考虑补充细节实现。")
        if any(token in compact for token in ("缺失", "不存在", "未", "导入", "import", "模块")):
            hints.append("本轮重点确保入口、导入路径与实际文件一一对应，可直接启动或至少可完成基础运行校验。")
        if any(token in compact for token in ("keyerror", "attributeerror", "typeerror", "assert", "unexpected keyword", "missing 1 required positional argument")):
            hints.append("若失败来自测试断言或调用异常，优先对齐公开接口契约：函数签名、返回结构、字段名必须与现有测试和调用方一致，不要只修局部逻辑。")
        if any(token in compact for token in ("pytest", "test_", "smoke", "接口", "验收")):
            hints.append("优先修复当前测试直接指出的问题；除非明确要求，不要通过修改测试来规避失败。")
    elif normalized_type == "testing":
        hints.append("优先修复会直接导致测试失败或无法验证的问题，保持测试报告可复现。")
    if attempt > 0:
        hints.append(f"这是第 {attempt + 1} 次评审返工，请先把上述阻塞项彻底收敛，再提交下一版。")
    return "\n".join(f"- {item}" for item in hints if item)


def _normalize_decision_options(value: Any, *, limit: int = 4) -> List[str]:
    if not isinstance(value, list):
        return []
    options: List[str] = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        options.append(text)
        if len(options) >= limit:
            break
    return options


def _extract_agent_decision_candidates(payload: Dict[str, Any]) -> Dict[str, Any] | None:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    candidate = metadata.get("human_decision") if isinstance(metadata.get("human_decision"), dict) else {}
    if not candidate:
        return None
    question = str(candidate.get("question") or "").strip()
    reason = str(candidate.get("reason") or candidate.get("why_blocked") or "").strip()
    options = _normalize_decision_options(candidate.get("options"))
    if not (question or reason or options):
        return None
    return {
        "question": question,
        "reason": reason,
        "options": options,
    }


def _extract_human_decision_request(
    stage_name: str,
    stage_type: str,
    stage_label: str,
    review: Dict[str, Any],
) -> Dict[str, Any] | None:
    if not isinstance(review, dict):
        return None
    required = review.get("human_decision_required")
    if required is None:
        required = review.get("requires_human_decision")
    if required is None and isinstance(review.get("human_decision"), dict):
        required = True
    if required is not True:
        return None
    request_payload = review.get("human_decision") if isinstance(review.get("human_decision"), dict) else review
    question = str(
        request_payload.get("decision_question")
        or request_payload.get("question")
        or review.get("feedback")
        or f"{stage_label} 需要人工决策"
    ).strip()
    reason = str(
        request_payload.get("decision_reason")
        or request_payload.get("why_blocked")
        or request_payload.get("reason")
        or review.get("feedback")
        or ""
    ).strip()
    options = _normalize_decision_options(
        request_payload.get("decision_options")
        or request_payload.get("options")
    )
    return {
        "kind": "human_decision",
        "stage": stage_name,
        "stage_type": stage_type,
        "label": stage_label,
        "question": question,
        "options": options,
        "why_blocked": reason,
        "requested_by": "leader_review",
    }


def _is_review_blocking(stage_type: str, stage_cfg: Dict[str, Any]) -> bool:
    raw = stage_cfg.get("review_blocking")
    if raw is None:
        return normalize_stage_type(stage_type) in DEFAULT_BLOCKING_REVIEW_STAGE_TYPES
    return bool(raw)


def _load_task_artifact_text(task: Task, rel_path: str) -> str:
    workspace = os.path.abspath(task.workspace_path or "")
    if not workspace:
        return ""
    abs_path = os.path.join(workspace, rel_path)
    if not os.path.exists(abs_path):
        return ""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _review_feedback_is_evidence_limited(feedback_text: str) -> bool:
    text = str(feedback_text or "")
    if not text:
        return False
    uncertain_markers = ("无法确认", "尚不能确认", "证据不足", "未见", "可能缺少", "大概率", "当前可见证据")
    return any(marker in text for marker in uncertain_markers)


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
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


def _infer_provider_type(base_url: str, model_name: str = "") -> str:
    base = str(base_url or "").lower()
    model = str(model_name or "").lower()
    if "generativelanguage.googleapis.com" in base or "/v1beta" in base and "googleapis" in base:
        return "gemini"
    if "codex" in base or "codex" in model or model.startswith("gpt-5"):
        return "codex"
    if model.startswith("gemini"):
        return "gemini"
    return "openai-compatible"


def _registry_provider_cfg(provider_id: str, model_row: Dict[str, Any], cred: Dict[str, Any], overrides: Dict[str, Any] | None = None) -> Dict[str, Any]:
    inferred_type = _infer_provider_type(cred.get("base_url") or "", model_row.get("name") or "")
    provider_type = model_row.get("provider_type") or inferred_type
    if provider_type == "openai-compatible" and inferred_type == "codex":
        provider_type = "codex"
    cfg = {
        "id": provider_id,
        "type": provider_type,
        "model": model_row.get("name") or "",
        "base_url": cred.get("base_url") or "",
        "api_key_env": cred.get("api_key_env") or None,
        "api_key": cred.get("api_key") or None,
        "label": f"{cred.get('name') or 'Registry'} / {model_row.get('name') or ''}",
    }
    extra = model_row.get("extra_config") or {}
    if isinstance(extra, dict):
        cfg.update({k: v for k, v in extra.items() if v not in (None, "")})
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None and v != ""})
    return cfg


class GraphBuilder:
    def __init__(
        self,
        base_dir: str,
        model_registry: ModelRegistry,
        storage=None,
        capability_settings_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        skill_settings_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        mcp_settings_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self.base_dir = base_dir
        self.model_registry = model_registry
        self.storage = storage or FileStore()
        self.capability_settings_provider = capability_settings_provider
        self.skill_settings_provider = skill_settings_provider
        self.mcp_settings_provider = mcp_settings_provider

    def _get_capability_settings(self) -> Dict[str, Any]:
        if callable(self.capability_settings_provider):
            try:
                return merge_capability_settings(self.capability_settings_provider())
            except Exception:
                return merge_capability_settings()
        return merge_capability_settings()

    def _get_skill_settings(self) -> Dict[str, Any]:
        if callable(self.skill_settings_provider):
            try:
                return merge_skill_settings(self.skill_settings_provider())
            except Exception:
                return merge_skill_settings()
        return merge_skill_settings()

    def _get_mcp_settings(self) -> Dict[str, Any]:
        if callable(self.mcp_settings_provider):
            try:
                return merge_mcp_settings(self.mcp_settings_provider())
            except Exception:
                return merge_mcp_settings()
        return merge_mcp_settings()

    def plan_workflow(self, task: Task, template: Dict[str, Any]) -> Dict[str, Any]:
        """Leader planning stage: dynamically design workflow per task."""
        planner_model = self._select_model(task, stage_name="planning", capabilities=[])
        spec = str((task.context or {}).get("spec") or "")
        capability_settings = self._get_capability_settings()
        skill_settings = self._get_skill_settings()
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

        planned_stages = _normalize_stage_plan(
            parsed.get("stages") if isinstance(parsed, dict) else [],
            spec,
            capability_settings=capability_settings,
            skill_settings=skill_settings,
        )
        used_fallback = not bool(planned_stages)
        if used_fallback:
            planned_stages = fallback_plan["stages"]

        event_configs = task.context.setdefault("event_configs", {})
        for stage in planned_stages:
            stage_name = str(stage.get("name") or "").strip()
            if not stage_name:
                continue
            stage_type = resolve_stage_execution_profile(stage)
            base_cfg = dict(event_configs.get(stage_type, {})) if stage_type != stage_name else dict(event_configs.get(stage_name, {}))
            stage_cfg = dict(base_cfg)
            stage_cfg.update(event_configs.get(stage_name, {}))
            stage_cfg["stage_type"] = stage_type
            stage_cfg["execution_profile"] = stage_type
            stage_cfg["stage_semantics"] = resolve_stage_semantics(stage, execution_profile=stage_type)
            if stage.get("prompt_template"):
                stage_cfg["prompt_template"] = stage["prompt_template"]
            if stage.get("role"):
                stage_cfg["planned_role"] = stage["role"]
            if stage.get("skills"):
                stage_cfg["planned_skills"] = list(stage["skills"])
            if stage.get("acceptance_criteria"):
                stage_cfg["acceptance_criteria"] = stage["acceptance_criteria"]
            event_configs[stage_name] = stage_cfg

        plan_meta = parsed if isinstance(parsed, dict) else {}
        conversation_groups = resolve_conversation_groups(planned_stages, plan_meta.get("conversation_groups"))
        task.context["event_configs"] = event_configs
        task.context["leader_plan"] = {
            "complexity": str(plan_meta.get("complexity") or fallback_plan.get("complexity") or "standard"),
            "reference_preset": str(plan_meta.get("reference_preset") or fallback_plan.get("reference_preset") or "custom"),
            "summary": str(plan_meta.get("summary") or fallback_plan.get("summary") or ""),
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

    def load_template(self, template_path: str) -> Dict[str, Any]:
        with open(template_path, "r") as f:
            return json.load(f)

    def load_agents(self, agents_yaml: str) -> Dict[str, Dict[str, Any]]:
        with open(agents_yaml, "r") as f:
            data = yaml.safe_load(f)
        return {a["id"]: a for a in data.get("agents", [])}

    def _select_model(self, task: Task, stage_name: str | None = None, capabilities: List[str] | None = None):
        context = task.context or {}
        event_configs = context.get("event_configs") or {}
        stage_cfg = event_configs.get(stage_name, {}) if stage_name else {}
        explicit_provider = (
            stage_cfg.get("model_provider")
            or context.get("default_model_provider")
        )
        model_overrides = {
            "model": stage_cfg.get("model"),
            "temperature": stage_cfg.get("temperature"),
            "timeout": stage_cfg.get("timeout"),
            "base_url": stage_cfg.get("base_url"),
            "api_key_env": stage_cfg.get("api_key_env"),
            "api_key": stage_cfg.get("api_key"),
        }
        has_overrides = any(v is not None and v != "" for v in model_overrides.values())

        if explicit_provider:
            if str(explicit_provider).startswith("registry:model:"):
                model_id = str(explicit_provider).split(":", 2)[-1]
                model_row = db.get_ai_model(model_id)
                cred = db.get_ai_credential_secret((model_row or {}).get("credential_id", "")) if model_row else None
                if model_row and cred:
                    cfg = _registry_provider_cfg(explicit_provider, model_row, cred, model_overrides if has_overrides else None)
                    return self.model_registry.build_adapter(cfg)
            return self.model_registry.get_by_id(explicit_provider, overrides=model_overrides if has_overrides else None)

        llm_models = [m for m in db.list_ai_models() if (m.get("model_kind") or "llm") == "llm"]
        if llm_models:
            model_id = llm_models[0].get("model_id", "")
            model_row = db.get_ai_model(model_id)
            cred = db.get_ai_credential_secret((model_row or {}).get("credential_id", "")) if model_row else None
            if model_row and cred:
                cfg = _registry_provider_cfg(f"registry:model:{model_id}", model_row, cred, model_overrides if has_overrides else None)
                return self.model_registry.build_adapter(cfg)
        raise ValueError("未配置可用的 llm 模型，请先到 /models.html 注册并绑定模型")

    def _review_stage_output(self, task: Task, stage_name: str, payload: Dict[str, Any], stage_type: str | None = None, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        effective_stage_type = normalize_stage_type(stage_type or stage_name)
        event_configs = (task.context or {}).get("event_configs", {})
        stage_cfg = dict(event_configs.get(effective_stage_type, {})) if effective_stage_type != stage_name else {}
        stage_cfg.update(event_configs.get(stage_name, {}) if isinstance(event_configs, dict) else {})
        criteria = str(stage_cfg.get("acceptance_criteria") or "").strip()
        role = str(stage_cfg.get("planned_role") or "")
        summary = payload.get("output_summary") or {}
        artifacts = payload.get("artifacts") or []
        review_text_parts: List[str] = []
        total_chars = 0
        max_chars = 24000
        architecture_doc_text = ""
        docs_readme_text = ""
        workspace = os.path.abspath(task.workspace_path or os.path.join(self.base_dir, task.task_id))
        evidence_paths: List[str] = []
        seen_evidence = set()

        def add_evidence_path(path: str) -> None:
            abs_path = os.path.abspath(path)
            if abs_path in seen_evidence:
                return
            seen_evidence.add(abs_path)
            evidence_paths.append(abs_path)

        if effective_stage_type == "docs":
            add_evidence_path(os.path.join(workspace, "docs", "README.md"))
            add_evidence_path(os.path.join(workspace, "tests", "manual_test_report.md"))
            add_evidence_path(os.path.join(workspace, "analysis", "requirements.md"))
            add_evidence_path(os.path.join(workspace, "design", "architecture.md"))

        for art in artifacts[:12]:
            uri = str((art or {}).get("uri") or "")
            if not uri or uri == "inline":
                continue
            add_evidence_path(uri)

        for abs_uri in evidence_paths:
            if not abs_uri.startswith(workspace):
                continue
            low = abs_uri.lower()
            if not (
                low.endswith(".md")
                or low.endswith(".txt")
                or low.endswith(".py")
                or low.endswith(".json")
                or low.endswith(".html")
                or low.endswith(".css")
                or low.endswith(".js")
                or low.endswith(".ts")
                or low.endswith(".tsx")
                or low.endswith(".jsx")
            ):
                continue
            try:
                with open(abs_uri, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read(12000)
            except Exception:
                continue
            if not text:
                continue
            if effective_stage_type == "architecture" and abs_uri.lower().endswith("architecture.md"):
                architecture_doc_text = text
            if effective_stage_type == "docs" and abs_uri.lower().endswith(os.path.join("docs", "readme.md")):
                docs_readme_text = text
            left = max_chars - total_chars
            if left <= 0:
                break
            clipped = text[:left]
            review_text_parts.append(f"[{os.path.relpath(abs_uri, workspace)}]\n{clipped}")
            total_chars += len(clipped)

        review_text = "\n\n".join(review_text_parts)
        collaboration_text = CollaborationHub(task).build_stage_review_context(stage_name, local_limit=4, blackboard_limit=4, max_chars=3000)
        agent_decision_candidate = _extract_agent_decision_candidates(payload)
        smoke_failed = self._artifact_command_failed(payload, {"smoke_test_result"})
        test_failed = self._artifact_command_failed(payload, {"test_result", "compile_result"})
        validation_signals: List[str] = []
        architecture_issues: List[str] = []
        docs_issues: List[str] = []
        if effective_stage_type == "coding":
            validation_signals.append(f"编码阶段冒烟结果：{'失败' if smoke_failed else '通过'}")
        if effective_stage_type == "testing":
            validation_signals.append(f"测试阶段执行结果：{'失败' if test_failed else '通过'}")
        if effective_stage_type == "architecture" and architecture_doc_text:
            requirements_text = _load_task_artifact_text(task, os.path.join("analysis", "requirements.md"))
            architecture_issues = _architecture_validation_issues(
                str((task.context or {}).get("spec") or ""),
                requirements_text,
                architecture_doc_text,
            )
            if architecture_issues:
                validation_signals.append("架构文档结构校验未通过：" + "；".join(architecture_issues))
        if effective_stage_type == "docs" and docs_readme_text:
            docs_issues = _docs_validation_issues(docs_readme_text)
            if docs_issues:
                validation_signals.append("README 结构校验未通过：" + "；".join(docs_issues))
            else:
                validation_signals.append("README 结构校验通过：已检测到运行方式、文件结构、限制说明与测试结论。")
        if review_text_parts:
            validation_signals.append("注意：下方文件片段可能因长度限制被截断，不能仅凭片段结尾不完整就认定源文件本身被截断；应结合编译/测试结果综合判断。")

        if effective_stage_type == "testing":
            has_manual_report = any(str((art or {}).get("uri") or "").endswith("manual_test_report.md") for art in artifacts)
            workspace = os.path.abspath(task.workspace_path or os.path.join(self.base_dir, task.task_id))
            source_files: List[str] = []
            for root_dir, _, file_names in os.walk(workspace):
                for file_name in file_names:
                    if file_name.endswith((".py", ".html", ".css", ".js", ".ts", ".tsx", ".jsx")):
                        source_files.append(os.path.join(root_dir, file_name))
            manual_report_missing_code = (
                "未发现 Python 源文件" in review_text
                or "未发现可执行源码文件" in review_text
            )
            if has_manual_report and source_files and not manual_report_missing_code and not self._artifact_command_failed(payload, {"test_result", "compile_result"}):
                has_web_source = any(path.endswith((".html", ".css", ".js", ".ts", ".tsx", ".jsx")) for path in source_files)
                feedback = "未发现可执行自动化用例，已按回退策略完成源码编译校验并生成手工测试清单，本轮测试阶段可暂时验收。"
                risks = [
                    "当前仍以编译校验和手工测试清单为主，后续迭代建议补充自动化测试。",
                    "UI 与交互体验仍需人工走查确认。",
                ]
                next_actions = [
                    "按 manual_test_report.md 执行关键玩法与 UI 手工验收。",
                    "后续补充至少一组核心逻辑自动化测试用例。",
                ]
                if has_web_source:
                    feedback = "未发现可执行自动化用例，已按回退策略完成 Web 静态校验并生成手工测试清单，本轮测试阶段可暂时验收。"
                    risks = [
                        "当前仍以静态校验和手工测试清单为主，真实浏览器交互仍需人工确认。",
                        "后续迭代建议补充浏览器侧自动化冒烟或交互测试。",
                    ]
                    next_actions = [
                        "按 manual_test_report.md 执行关键玩法、交互与控制台错误手工验收。",
                        "后续补充至少一组浏览器侧自动化测试或脚本化冒烟验证。",
                    ]
                return {
                    "review_status": "fallback",
                    "pass": True,
                    "score": 0.72,
                    "feedback": feedback,
                    "risks": risks,
                    "next_actions": next_actions,
                    "criteria": criteria,
                    "role": role,
                }

        if not criteria:
            return {
                "review_status": "skipped",
                "pass": None,
                "score": None,
                "feedback": "未配置验收标准，跳过自动评审。",
                "criteria": "",
                "role": role,
            }

        review_prompt = (
            "你是团队Leader，负责评审阶段产出是否满足验收标准。请仅输出 JSON。\n"
            f"阶段：{stage_name}\n"
            f"阶段类型：{effective_stage_type}\n"
            f"角色：{role or '-'}\n"
            f"验收标准：{criteria}\n"
            f"验证信号：{json.dumps(validation_signals, ensure_ascii=False)}\n"
            f"阶段执行方提出的人工决策候选：{json.dumps(agent_decision_candidate or {}, ensure_ascii=False)}\n"
            f"阶段输出摘要：{json.dumps(summary, ensure_ascii=False)}\n"
            f"产物：{json.dumps(artifacts[:8], ensure_ascii=False)}\n"
            f"阶段关键内容片段：\n{review_text[:24000]}\n"
            f"阶段协作记录：\n{collaboration_text[:5000]}\n"
            "如果需要人工决策，请把 human_decision_required 设为 true，并补充 decision_question、decision_options、decision_reason；"
            "否则 human_decision_required 设为 false。\n"
            "输出格式："
            "{\"pass\":true,\"score\":0.0,\"feedback\":\"...\",\"risks\":[\"...\"],\"next_actions\":[\"...\"],\"human_decision_required\":false,\"decision_question\":\"...\",\"decision_options\":[\"...\"],\"decision_reason\":\"...\"}"
        )

        raw = ""
        try:
            reviewer = self._select_model(task, stage_name="planning", capabilities=[])
            _emit_stage_progress(
                progress_callback,
                progress_kind="review",
                progress_state="start",
                message="正在进行阶段评审",
            )
            raw = str(reviewer.generate(review_prompt, context=task.context))
            failure = _model_failure_text(raw)
            _emit_stage_progress(
                progress_callback,
                progress_kind="review",
                progress_state="error" if failure else "done",
                message=f"阶段评审{'失败' if failure else '完成'}",
                error=failure or None,
            )
            parsed = _extract_json_block(raw) or {}
        except Exception as e:
            parsed = {}
            raw = f"[review error] {e}"
            _emit_stage_progress(
                progress_callback,
                progress_kind="review",
                progress_state="error",
                message="阶段评审失败",
                error=str(e),
            )

        if isinstance(parsed, dict) and isinstance(parsed.get("pass"), bool):
            if architecture_issues:
                merged_feedback = str(parsed.get("feedback") or "").strip()
                parsed["pass"] = False
                parsed["feedback"] = (
                    ((merged_feedback + "\n\n") if merged_feedback else "")
                    + "架构文档存在确定性的结构问题："
                    + "；".join(architecture_issues)
                )
                risks = parsed.get("risks") if isinstance(parsed.get("risks"), list) else []
                for issue in architecture_issues:
                    if issue not in risks:
                        risks.append(issue)
                parsed["risks"] = risks
            if effective_stage_type == "docs":
                merged_feedback = str(parsed.get("feedback") or "").strip()
                if docs_issues:
                    parsed["pass"] = False
                    parsed["feedback"] = (
                        ((merged_feedback + "\n\n") if merged_feedback else "")
                        + "README 存在确定性的结构问题："
                        + "；".join(docs_issues)
                    )
                    risks = parsed.get("risks") if isinstance(parsed.get("risks"), list) else []
                    for issue in docs_issues:
                        if issue not in risks:
                            risks.append(issue)
                    parsed["risks"] = risks
                elif parsed.get("pass") is False and _review_feedback_is_evidence_limited(merged_feedback):
                    parsed["pass"] = True
                    parsed["feedback"] = (
                        "已基于 README 正文执行确定性结构校验，确认其覆盖运行方式、文件结构、限制说明与测试结论；"
                        "本轮将“证据不足型误判”自动纠正为通过。"
                        + ((f"\n\n原评审反馈：{merged_feedback}") if merged_feedback else "")
                    )
            return {
                "review_status": "ok",
                "pass": parsed.get("pass"),
                "score": parsed.get("score"),
                "feedback": parsed.get("feedback", ""),
                "risks": parsed.get("risks", []),
                "next_actions": parsed.get("next_actions", []),
                "human_decision_required": parsed.get("human_decision_required") is True,
                "decision_question": parsed.get("decision_question") or "",
                "decision_options": parsed.get("decision_options", []),
                "decision_reason": parsed.get("decision_reason") or "",
                "criteria": criteria,
                "role": role,
                "raw": raw[:1200],
            }

        artifact_count = int(summary.get("artifact_count") or 0)
        guessed_pass = artifact_count > 0 and not architecture_issues
        return {
            "review_status": "fallback",
            "pass": guessed_pass,
            "score": 0.55 if guessed_pass else 0.25,
            "feedback": (
                "评审模型返回非 JSON，使用启发式评估（按产物数量）。"
                if not architecture_issues
                else "评审模型返回非 JSON，且架构文档存在确定性的结构问题：" + "；".join(architecture_issues)
            ),
            "criteria": criteria,
            "role": role,
            "raw": raw[:1200],
        }

    def _cleanup_artifacts(self, task: Task, artifacts: List[Dict[str, Any]]):
        workspace = os.path.abspath(task.workspace_path or os.path.join(self.base_dir, task.task_id))
        for art in artifacts or []:
            uri = str((art or {}).get("uri") or "")
            if not uri or uri == "inline":
                continue
            abs_uri = os.path.abspath(uri)
            if not abs_uri.startswith(workspace):
                continue
            try:
                if os.path.isdir(abs_uri):
                    shutil.rmtree(abs_uri, ignore_errors=True)
                elif os.path.exists(abs_uri):
                    os.remove(abs_uri)
            except Exception:
                continue

    def _testing_failed(self, payload: Dict[str, Any]) -> bool:
        return self._artifact_command_failed(payload, {"test_result", "compile_result"})

    def _smoke_test_failed(self, payload: Dict[str, Any]) -> bool:
        return self._artifact_command_failed(payload, {"smoke_test_result"})

    def _artifact_command_failed(self, payload: Dict[str, Any], artifact_types: set[str]) -> bool:
        for art in (payload.get("artifacts") or []):
            if (art or {}).get("type") not in artifact_types:
                continue
            content = (art or {}).get("content") or {}
            if isinstance(content, dict):
                exit_code = content.get("exit_code")
                try:
                    if exit_code is not None and int(exit_code) != 0:
                        return True
                except Exception:
                    return True
        return False

    def _collect_test_feedback(self, payload: Dict[str, Any]) -> str:
        return self._collect_command_feedback(payload, {"test_result", "compile_result"})

    def _collect_smoke_feedback(self, payload: Dict[str, Any]) -> str:
        return self._collect_command_feedback(payload, {"smoke_test_result"})

    def _collect_command_feedback(self, payload: Dict[str, Any], artifact_types: set[str]) -> str:
        parts: List[str] = []
        for art in (payload.get("artifacts") or []):
            if (art or {}).get("type") not in artifact_types:
                continue
            content = (art or {}).get("content") or {}
            if not isinstance(content, dict):
                continue
            cmd = str(content.get("command") or "")
            stderr = str(content.get("stderr") or "")[:2000]
            stdout = str(content.get("stdout") or "")[:1000]
            code = content.get("exit_code")
            parts.append(f"cmd={cmd} exit={code}\nstderr={stderr}\nstdout={stdout}")
        return "\n\n".join(parts)[:5000]

    def build(self, task: Task, template: Dict[str, Any], stage_logger=None, should_abort: Callable[[Task], bool] | None = None) -> StateGraph:
        """Build a LangGraph flow from a dynamic stage plan."""
        sg = StateGraph(dict)
        capability_settings = self._get_capability_settings()
        skill_settings = self._get_skill_settings()
        capability_runtime = CapabilityRuntime(
            capability_settings=capability_settings,
            mcp_settings=self._get_mcp_settings(),
        )
        capability_index = capability_runtime.capability_index
        stages = [dict(st) for st in (template.get("stages") or []) if isinstance(st, dict) and st.get("name")]
        if not stages:
            raise ValueError("workflow has no stages")

        stage_defs_by_name = {str(st["name"]): dict(st) for st in stages}
        ordered_stage_names = [str(st["name"]) for st in stages]
        stage_types_by_name = {name: resolve_stage_execution_profile(stage_defs_by_name[name]) for name in ordered_stage_names}
        stage_labels = {name: stage_defs_by_name[name].get("label", name) for name in ordered_stage_names}
        stage_skills = {name: stage_defs_by_name[name].get("skills", []) for name in ordered_stage_names}
        stage_caps = {name: stage_defs_by_name[name].get("capabilities", []) for name in ordered_stage_names}
        stages_by_type: Dict[str, List[str]] = {}
        for name in ordered_stage_names:
            stages_by_type.setdefault(stage_types_by_name[name], []).append(name)

        def get_stage_cfg(stage_name: str) -> Dict[str, Any]:
            raw_event_configs = (task.context or {}).get("event_configs") or {}
            stage_type = stage_types_by_name.get(stage_name, normalize_stage_type(stage_name))
            cfg = dict(raw_event_configs.get(stage_type, {})) if stage_type != stage_name else {}
            cfg.update(raw_event_configs.get(stage_name, {}))
            cfg["stage_type"] = stage_type
            return cfg

        def resolve_related_stage(anchor_stage: str, target_type: str, prefer_prior: bool = True) -> str | None:
            normalized = normalize_stage_type(target_type)
            candidates = stages_by_type.get(normalized, [])
            if not candidates:
                return None
            if anchor_stage not in ordered_stage_names:
                return candidates[0]
            anchor_index = ordered_stage_names.index(anchor_stage)
            indexed = [(ordered_stage_names.index(name), name) for name in candidates]
            if prefer_prior:
                prior = [name for idx, name in indexed if idx < anchor_index]
                if prior:
                    return prior[-1]
            if anchor_stage in candidates:
                return anchor_stage
            later = [name for idx, name in indexed if idx >= anchor_index]
            if later:
                return later[0]
            return candidates[-1]

        def stage_invokable_capabilities(stage_name: str) -> List[Dict[str, Any]]:
            resolved: List[Dict[str, Any]] = []
            seen = set()
            stage_type = stage_types_by_name.get(stage_name, normalize_stage_type(stage_name))
            candidate_ids = list(stage_caps.get(stage_name, []) or [])
            for capability_id, capability_def in capability_index.items():
                if capability_id in seen:
                    continue
                recommended = capability_def.get("recommended_stage_types") or []
                if capability_def.get("enabled", True) and capability_def.get("planner_visible", True) and stage_type in recommended:
                    candidate_ids.append(capability_id)
            for capability_id in candidate_ids:
                normalized_id = str(capability_id or "").strip()
                if not normalized_id or normalized_id in seen:
                    continue
                capability_def = capability_index.get(normalized_id)
                if not isinstance(capability_def, dict):
                    continue
                if capability_def.get("enabled", True) is False:
                    continue
                seen.add(normalized_id)
                resolved.append(dict(capability_def))
            return resolved

        def create_agent(stage_name: str):
            stage_def = stage_defs_by_name.get(stage_name, {"name": stage_name, "stage_type": normalize_stage_type(stage_name)})
            stage_type = stage_types_by_name.get(stage_name, normalize_stage_type(stage_def.get("stage_type") or stage_name))
            model_adapter = self._select_model(task, stage_name=stage_name, capabilities=stage_caps.get(stage_name, []))
            stage_cfg = get_stage_cfg(stage_name)
            prompt_template = stage_cfg.get("prompt_template")
            progress_callback = None
            if stage_logger:
                progress_callback = lambda payload: stage_logger(stage_name, "progress", {
                    "label": stage_labels.get(stage_name, stage_name),
                    "stage_type": stage_type,
                    **(payload or {}),
                })
            if stage_type == "requirements":
                return ReqAgent(model_adapter, stage_name=stage_name, stage_type=stage_type, prompt_template=prompt_template, progress_callback=progress_callback)
            if stage_type == "architecture":
                return ArchAgent(model_adapter, stage_name=stage_name, stage_type=stage_type, prompt_template=prompt_template, progress_callback=progress_callback)
            if stage_type == "assets":
                return AssetAgent(model_adapter, stage_name=stage_name, stage_type=stage_type, prompt_template=prompt_template, progress_callback=progress_callback)
            if stage_type == "coding":
                return PatchAgent(model_adapter=model_adapter, stage_name=stage_name, stage_type=stage_type, progress_callback=progress_callback)
            if stage_type == "testing":
                return TestAgent(stage_name=stage_name, stage_type=stage_type, progress_callback=progress_callback)
            if stage_type == "docs":
                return DocAgent(model_adapter, stage_name=stage_name, stage_type=stage_type, prompt_template=prompt_template, progress_callback=progress_callback)
            raise ValueError(f"unknown stage type: {stage_type}")

        def make_node(stage_name: str, human_checkpoint: bool):
            current_stage_type = stage_types_by_name.get(stage_name, normalize_stage_type(stage_name))

            def node(state: Dict[str, Any]):
                if state.get("error"):
                    return state
                if should_abort and should_abort(state["task"]):
                    state["abort"] = {"stage": stage_name, "stage_type": current_stage_type, "reason": "task_aborted"}
                    if stage_logger:
                        stage_logger(stage_name, "abort", {"label": stage_labels.get(stage_name, stage_name), "stage_type": current_stage_type, "reason": "task_aborted"})
                    return state
                if human_checkpoint and not state.get("resume", False):
                    state["await"] = {"stage": stage_name, "stage_type": current_stage_type, "label": stage_labels.get(stage_name, stage_name)}
                    if stage_logger:
                        stage_logger(stage_name, "await", {"label": stage_labels.get(stage_name, stage_name), "stage_type": current_stage_type})
                    return state
                task_obj: Task = state["task"]
                collaboration = CollaborationHub(task_obj)

                def default_actor_id(target_stage: str, target_type: str) -> str:
                    mapping = {
                        "requirements": "req-analyst",
                        "architecture": "architect",
                        "assets": "asset-designer",
                        "coding": "patcher",
                        "testing": "tester",
                        "docs": "doc-writer",
                    }
                    return mapping.get(target_type, target_stage)

                def stage_role_name(target_stage: str, target_type: str, fallback: str = "") -> str:
                    stage_cfg = get_stage_cfg(target_stage)
                    stage_def = stage_defs_by_name.get(target_stage, {})
                    return str(stage_cfg.get("planned_role") or stage_def.get("role") or fallback or target_type)

                def apply_runtime_collaboration_context(target_stage: str) -> None:
                    prompt_context = collaboration.build_stage_prompt_context(target_stage)
                    skill_context = build_skill_runtime_context(stage_skills.get(target_stage, []), skill_settings)
                    capability_context = build_capability_invoke_prompt(stage_invokable_capabilities(target_stage))
                    prompt_context = "\n\n".join(
                        part for part in [prompt_context, skill_context, capability_context] if str(part or "").strip()
                    )
                    task_obj.context["_runtime_collaboration"] = {
                        "stage_name": target_stage,
                        "prompt_context": prompt_context,
                        "selection_context": collaboration.build_stage_targeted_context(target_stage),
                    }

                def clear_runtime_collaboration_context() -> None:
                    task_obj.context.pop("_runtime_collaboration", None)

                def record_stage_submission(
                    target_stage: str,
                    target_type: str,
                    payload: Dict[str, Any],
                    *,
                    actor_id: str,
                    actor_role: str,
                ) -> str:
                    conversation_id = collaboration.ensure_thread(
                        target_stage,
                        stage_type=target_type,
                        thread_kind="stage_loop",
                        title=f"{stage_labels.get(target_stage, target_stage)} 协作线程",
                        participants=[
                            {"actor_id": actor_id, "role": actor_role},
                            {"actor_id": f"{target_stage}-reviewer", "role": "阶段评审"},
                        ],
                    )
                    submission = collaboration.post_message(
                        stage_name=target_stage,
                        stage_type=target_type,
                        actor_id=actor_id,
                        actor_role=actor_role,
                        content=CollaborationHub.summarize_submission(payload),
                        message_type="submission",
                        conversation_id=conversation_id,
                        recipient_id=f"{target_stage}-reviewer",
                        payload={"output_summary": payload.get("output_summary") or {}},
                    )
                    collaboration.upsert_blackboard(
                        entry_key=f"stage:{target_stage}:delivery",
                        title=f"{stage_labels.get(target_stage, target_stage)} 最新交付",
                        content=CollaborationHub.summarize_submission(payload),
                        entry_type="stage_delivery",
                        stage_name=target_stage,
                        payload={"output_summary": payload.get("output_summary") or {}},
                        source_message_id=submission.get("message_id"),
                    )
                    review = payload.get("review") or {}
                    if isinstance(review, dict) and review.get("review_status") != "skipped":
                        review_message = collaboration.post_message(
                            stage_name=target_stage,
                            stage_type=target_type,
                            actor_id=f"{target_stage}-reviewer",
                            actor_role="阶段评审",
                            content=CollaborationHub.summarize_review(review),
                            message_type="review_feedback",
                            conversation_id=conversation_id,
                            recipient_id=actor_id,
                            reply_to=submission.get("message_id"),
                            payload=review,
                        )
                        collaboration.upsert_blackboard(
                            entry_key=f"stage:{target_stage}:review",
                            title=f"{stage_labels.get(target_stage, target_stage)} 最新评审",
                            content=CollaborationHub.summarize_review(review),
                            entry_type="stage_review",
                            stage_name=target_stage,
                            payload=review,
                            source_message_id=review_message.get("message_id"),
                        )
                        decision_payload = {
                            "pass": review.get("pass") is True,
                            "stage_type": target_type,
                            "output_summary": payload.get("output_summary") or {},
                            "review_feedback": review.get("feedback"),
                            "next_actions": review.get("next_actions") or [],
                            "risks": review.get("risks") or [],
                        }
                        decision_content = (
                            CollaborationHub.summarize_decision_memory(
                                stage_labels.get(target_stage, target_stage),
                                payload,
                                review,
                            )
                            if review.get("pass") is True
                            else f"{stage_labels.get(target_stage, target_stage)} 评审未通过，原结论暂不固化为长期记忆。"
                        )
                        collaboration.upsert_blackboard(
                            entry_key=f"stage:{target_stage}:decision_memory",
                            title=f"{stage_labels.get(target_stage, target_stage)} 决策记忆",
                            content=decision_content,
                            entry_type="decision_memory",
                            stage_name=target_stage,
                            payload=decision_payload,
                            source_message_id=review_message.get("message_id"),
                        )
                        human_decision = payload.get("human_decision_request") if isinstance(payload.get("human_decision_request"), dict) else None
                        if human_decision:
                            decision_message = collaboration.post_message(
                                stage_name=target_stage,
                                stage_type=target_type,
                                actor_id=f"{target_stage}-reviewer",
                                actor_role="阶段评审",
                                content=(
                                    f"需要人工决策后才能继续推进。\n"
                                    f"问题：{human_decision.get('question')}\n"
                                    + (
                                        "可选方案：" + "；".join([str(item) for item in (human_decision.get("options") or []) if str(item).strip()])
                                        if human_decision.get("options")
                                        else "请直接在对话框中给出你的决定与约束。"
                                    )
                                ).strip(),
                                message_type="review_feedback",
                                conversation_id=conversation_id,
                                recipient_id="user",
                                reply_to=submission.get("message_id"),
                                payload=human_decision,
                            )
                            collaboration.upsert_blackboard(
                                entry_key=f"stage:{target_stage}:human_decision_request",
                                title=f"{stage_labels.get(target_stage, target_stage)} 待人工决策",
                                content=(
                                    f"{human_decision.get('question')}\n"
                                    + (f"原因：{human_decision.get('why_blocked')}" if human_decision.get("why_blocked") else "")
                                ).strip(),
                                entry_type="human_decision_request",
                                stage_name=target_stage,
                                payload={**human_decision, "resolved": False},
                                source_message_id=decision_message.get("message_id"),
                            )
                    return conversation_id

                def post_stage_status(
                    target_stage: str,
                    target_type: str,
                    content: str,
                    *,
                    conversation_id: str | None = None,
                    status_kind: str,
                    status_level: str = "active",
                    actor_id: str = "system",
                    actor_role: str = "流程状态",
                    recipient_id: str | None = None,
                    payload: Dict[str, Any] | None = None,
                ) -> str:
                    resolved_conversation_id = str(conversation_id or collaboration.ensure_thread(
                        target_stage,
                        stage_type=target_type,
                        thread_kind="stage_loop",
                        title=f"{stage_labels.get(target_stage, target_stage)} 协作线程",
                    ))
                    collaboration.post_message(
                        stage_name=target_stage,
                        stage_type=target_type,
                        actor_id=actor_id,
                        actor_role=actor_role,
                        content=content,
                        message_type="system_status",
                        conversation_id=resolved_conversation_id,
                        recipient_id=recipient_id,
                        payload={
                            "status_kind": status_kind,
                            "status_level": status_level,
                            **(payload or {}),
                        },
                    )
                    return resolved_conversation_id

                def to_payload(exec_result: Any) -> Dict[str, Any]:
                    payload: Dict[str, Any] = {}
                    if isinstance(exec_result, AgentMessage):
                        for art in exec_result.artifacts:
                            if art.get("uri") is None and art.get("content"):
                                fname = art.get("filename") or "artifact.txt"
                                path = write_text(self.base_dir, task_obj.task_id, fname, art["content"])
                                art["uri"] = path
                            state.setdefault("artifacts", []).append(art)
                        payload = {
                            "artifacts": exec_result.artifacts,
                            "metadata": exec_result.metadata,
                            "intent": exec_result.intent,
                            "actor": exec_result.actor_id,
                            "output_summary": {
                                "intent": exec_result.intent,
                                "artifact_count": len(exec_result.artifacts),
                                "artifact_types": [a.get("type") for a in exec_result.artifacts[:8]],
                                "artifact_uris": [a.get("uri") for a in exec_result.artifacts[:6]],
                            },
                        }
                        return payload
                    stage_artifacts: List[Dict[str, Any]] = []
                    if exec_result["type"] in {"md", "code"}:
                        path = write_text(self.base_dir, task_obj.task_id, exec_result["filename"], exec_result["content"])
                        stage_artifact = {"uri": path, "type": exec_result["type"]}
                        stage_artifacts.append(stage_artifact)
                        state.setdefault("artifacts", []).append(stage_artifact)
                    content_preview = str(exec_result.get("content", ""))[:700]
                    payload = {
                        "artifacts": stage_artifacts,
                        "output_summary": {
                            "result_type": exec_result.get("type"),
                            "filename": exec_result.get("filename"),
                            "artifact_count": len(stage_artifacts),
                            "artifact_types": [a.get("type") for a in stage_artifacts[:8]],
                            "artifact_uris": [a.get("uri") for a in stage_artifacts[:6]],
                            "content_preview": content_preview,
                            "content_length": len(str(exec_result.get("content", ""))),
                        },
                    }
                    return payload

                def write_runtime_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
                    normalized = dict(artifact or {})
                    uri = str(normalized.get("uri") or "").strip()
                    if uri and uri != "inline":
                        state.setdefault("artifacts", []).append(normalized)
                        return normalized
                    filename = str(normalized.get("filename") or "").strip()
                    if not filename:
                        return normalized
                    if "data" in normalized and isinstance(normalized.get("data"), (bytes, bytearray)):
                        uri = self.storage.put(task_obj.task_id, filename, bytes(normalized.get("data") or b""))
                    elif normalized.get("content") is not None:
                        content = normalized.get("content")
                        if isinstance(content, bytes):
                            uri = self.storage.put(task_obj.task_id, filename, content)
                        else:
                            uri = write_text(self.base_dir, task_obj.task_id, filename, str(content))
                    else:
                        return normalized
                    normalized["uri"] = uri
                    normalized.pop("data", None)
                    state.setdefault("artifacts", []).append(normalized)
                    return normalized

                def execute_stage_once(target_stage: str, reason: str | None = None) -> Dict[str, Any] | None:
                    target_type = stage_types_by_name.get(target_stage, normalize_stage_type(target_stage))
                    target_cfg = get_stage_cfg(target_stage)
                    actor_id = default_actor_id(target_stage, target_type)
                    actor_role = stage_role_name(target_stage, target_type, fallback=target_type)
                    if target_type == "coding" and reason in {"review_rework", "smoke_fix", "fix_from_testing", "after_architecture_rework"}:
                        orphan_removed = cleanup_architecture_orphan_files(
                            task_obj,
                            self.base_dir,
                            target_stage,
                            {"stage_type": target_type},
                        )
                        if orphan_removed and stage_logger:
                            stage_logger(target_stage, "progress", {
                                "label": stage_labels.get(target_stage, target_stage),
                                "stage_type": target_type,
                                "progress_kind": "cleanup",
                                "progress_state": "done",
                                "reason": reason,
                                "removed_orphans": orphan_removed,
                                "message": f"已清理 {orphan_removed} 个架构清单外的废弃文件",
                            })
                    if should_abort and should_abort(task_obj):
                        state["abort"] = {"stage": target_stage, "stage_type": target_type, "reason": "task_aborted"}
                        if stage_logger:
                            stage_logger(target_stage, "abort", {"label": stage_labels.get(target_stage, target_stage), "stage_type": target_type, "reason": "task_aborted"})
                        return None
                    apply_runtime_collaboration_context(target_stage)
                    conversation_id = None
                    capability_directives: List[Dict[str, Any]] = []
                    try:
                        agent = create_agent(target_stage)
                        actor_id = getattr(agent, "id", actor_id)
                        actor_role = getattr(agent, "role_name", actor_role) or actor_role
                        conversation_id = post_stage_status(
                            target_stage,
                            target_type,
                            f"{stage_labels.get(target_stage, target_stage)} 已交给 {actor_role}，等待模型返回。",
                            status_kind="agent_waiting",
                            status_level="waiting",
                            actor_id=f"{target_stage}-system",
                            payload={"reason": reason or "", "planned_role": actor_role},
                            recipient_id=actor_id,
                        )
                        if stage_logger:
                            start_payload = {"label": stage_labels.get(target_stage, target_stage), "stage_type": target_type}
                            if reason:
                                start_payload["reason"] = reason
                            stage_logger(target_stage, "start", start_payload)
                        exec_result = agent.act(task_obj, SystemState())
                        exec_result, capability_directives = extract_capability_invocations(exec_result)
                        conversation_id = post_stage_status(
                            target_stage,
                            target_type,
                            f"{stage_labels.get(target_stage, target_stage)} 已收到模型返回，正在整理产物与上下文。",
                            conversation_id=conversation_id,
                            status_kind="agent_returned",
                            status_level="active",
                            actor_id=f"{target_stage}-system",
                            payload={"planned_role": actor_role},
                        )
                        if capability_directives and stage_logger:
                            stage_logger(target_stage, "progress", {
                                "label": stage_labels.get(target_stage, target_stage),
                                "stage_type": target_type,
                                "progress_kind": "capability_invoke",
                                "progress_state": "detected",
                                "count": len(capability_directives),
                                "message": f"检测到 {len(capability_directives)} 个能力调用请求",
                            })
                    except Exception as e:
                        clear_runtime_collaboration_context()
                        err_text = str(e)
                        if conversation_id:
                            post_stage_status(
                                target_stage,
                                target_type,
                                f"{stage_labels.get(target_stage, target_stage)} 执行失败：{err_text}",
                                conversation_id=conversation_id,
                                status_kind="agent_error",
                                status_level="error",
                                actor_id=f"{target_stage}-system",
                                payload={"error": err_text},
                            )
                        if target_type == "coding" and err_text.startswith(("architecture_missing_doc:", "architecture_missing_file_list_section:", "architecture_invalid_file_list:")):
                            architecture_stage = resolve_related_stage(target_stage, "architecture", prefer_prior=True)
                            if not architecture_stage:
                                if stage_logger:
                                    stage_logger(target_stage, "error", {"error": err_text, "label": stage_labels.get(target_stage, target_stage), "stage_type": target_type})
                                state["error"] = err_text
                                return None
                            arch_attempts = int(state.setdefault("arch_rework_attempts", {}).get(architecture_stage, 0)) + 1
                            state.setdefault("arch_rework_attempts", {})[architecture_stage] = arch_attempts
                            if stage_logger:
                                stage_logger(architecture_stage, "rework", {
                                    "label": stage_labels.get(architecture_stage, architecture_stage),
                                    "stage_type": stage_types_by_name.get(architecture_stage, "architecture"),
                                    "attempt": arch_attempts,
                                    "reason": "coding_prerequisite_missing",
                                    "feedback": err_text,
                                })
                            architecture_conversation = collaboration.ensure_thread(
                                architecture_stage,
                                stage_type="architecture",
                                thread_kind="prerequisite_rework",
                                peer_stage=target_stage,
                                title=f"{stage_labels.get(architecture_stage, architecture_stage)} 前置条件修复",
                                participants=[
                                    {"actor_id": actor_id, "role": actor_role},
                                    {"actor_id": "architect", "role": stage_role_name(architecture_stage, "architecture", "架构设计师")},
                                ],
                            )
                            feedback_message = collaboration.post_message(
                                stage_name=architecture_stage,
                                stage_type="architecture",
                                actor_id=actor_id,
                                actor_role=actor_role,
                                content=(
                                    "编码阶段发现架构前置条件缺失，需要返工架构文件清单与模块职责。\n"
                                    f"失败信息：{err_text}"
                                ),
                                message_type="prerequisite_feedback",
                                conversation_id=architecture_conversation,
                                thread_kind="prerequisite_rework",
                                recipient_id="architect",
                                payload={"source_stage": target_stage, "error": err_text},
                            )
                            collaboration.upsert_blackboard(
                                entry_key=f"stage:{architecture_stage}:prerequisite_gap",
                                title=f"{stage_labels.get(architecture_stage, architecture_stage)} 前置缺口",
                                content=f"{stage_labels.get(target_stage, target_stage)} 反馈：{err_text}",
                                entry_type="prerequisite_gap",
                                stage_name=architecture_stage,
                                payload={"source_stage": target_stage, "error": err_text},
                                source_message_id=feedback_message.get("message_id"),
                            )
                            post_stage_status(
                                architecture_stage,
                                "architecture",
                                f"{stage_labels.get(target_stage, target_stage)} 发现前置条件缺口，已转交架构阶段返工。",
                                conversation_id=architecture_conversation,
                                status_kind="prerequisite_rework",
                                status_level="warning",
                                actor_id=f"{architecture_stage}-system",
                                payload={"source_stage": target_stage, "error": err_text},
                            )
                            return execute_stage_once(architecture_stage, reason=f"{target_stage}_prerequisite_rework")
                        if stage_logger:
                            stage_logger(target_stage, "error", {"error": err_text, "label": stage_labels.get(target_stage, target_stage), "stage_type": target_type})
                        state["error"] = err_text
                        return None
                    finally:
                        clear_runtime_collaboration_context()

                    payload = to_payload(exec_result)
                    payload["stage"] = target_stage
                    payload["stage_type"] = target_type
                    payload["label"] = stage_labels.get(target_stage, target_stage)
                    if target_cfg.get("planned_role"):
                        payload["planned_role"] = target_cfg.get("planned_role")
                    if target_cfg.get("acceptance_criteria"):
                        payload["acceptance_criteria"] = target_cfg.get("acceptance_criteria")
                    requested_capabilities, requested_capability_options = build_requested_capability_execution(capability_directives)
                    if capability_directives:
                        payload["requested_capability_invocations"] = capability_directives
                    capability_progress = None
                    if stage_logger:
                        capability_progress = lambda cap_payload: stage_logger(target_stage, "progress", {
                            "label": stage_labels.get(target_stage, target_stage),
                            "stage_type": target_type,
                            "progress_kind": "capability",
                            **(cap_payload or {}),
                        })
                    executed_capability_ids = set()
                    if requested_capabilities:
                        request_stage_cfg = dict(target_cfg)
                        merged_options = dict(target_cfg.get("capability_options") or {}) if isinstance(target_cfg.get("capability_options"), dict) else {}
                        for capability_id, options in requested_capability_options.items():
                            merged_entry = dict(merged_options.get(capability_id) or {})
                            merged_entry.update(options)
                            merged_options[capability_id] = merged_entry
                        request_stage_cfg["capability_options"] = merged_options
                        payload = capability_runtime.apply_stage_capabilities(
                            task=task_obj,
                            state=state,
                            stage_name=target_stage,
                            stage_type=target_type,
                            stage_label=stage_labels.get(target_stage, target_stage),
                            stage_config=request_stage_cfg,
                            capabilities=requested_capabilities,
                            exec_result=exec_result,
                            payload=payload,
                            write_artifact=write_runtime_artifact,
                            progress_callback=capability_progress,
                        )
                        executed_capability_ids.update(requested_capabilities)
                    remaining_capabilities = [
                        capability_id for capability_id in (stage_caps.get(target_stage, []) or [])
                        if str(capability_id or "").strip() and str(capability_id or "").strip() not in executed_capability_ids
                    ]
                    payload = capability_runtime.apply_stage_capabilities(
                        task=task_obj,
                        state=state,
                        stage_name=target_stage,
                        stage_type=target_type,
                        stage_label=stage_labels.get(target_stage, target_stage),
                        stage_config=target_cfg,
                        capabilities=remaining_capabilities,
                        exec_result=exec_result,
                        payload=payload,
                        write_artifact=write_runtime_artifact,
                        progress_callback=capability_progress,
                    )
                    review_progress = None
                    if stage_logger:
                        review_progress = lambda review_payload: stage_logger(target_stage, "progress", {
                            "label": stage_labels.get(target_stage, target_stage),
                            "stage_type": target_type,
                            **(review_payload or {}),
                        })
                    conversation_id = post_stage_status(
                        target_stage,
                        target_type,
                        f"{stage_labels.get(target_stage, target_stage)} 产物已生成，正在发起阶段评审。",
                        conversation_id=conversation_id,
                        status_kind="review_started",
                        status_level="active",
                        actor_id=f"{target_stage}-system",
                    )
                    payload["review"] = self._review_stage_output(task_obj, target_stage, payload, stage_type=target_type, progress_callback=review_progress)
                    payload["human_decision_request"] = _extract_human_decision_request(
                        target_stage,
                        target_type,
                        stage_labels.get(target_stage, target_stage),
                        payload.get("review") or {},
                    )
                    review = payload.get("review") or {}
                    review_text = "评审已完成。"
                    review_level = "done"
                    if isinstance(review, dict) and review.get("review_status") == "skipped":
                        review_text = f"{stage_labels.get(target_stage, target_stage)} 跳过了阶段评审。"
                        review_level = "active"
                    elif isinstance(review, dict) and review.get("pass") is True:
                        review_text = f"{stage_labels.get(target_stage, target_stage)} 评审通过，准备进入下一步。"
                        review_level = "done"
                    elif isinstance(review, dict) and review.get("pass") is False:
                        review_text = f"{stage_labels.get(target_stage, target_stage)} 评审未通过，等待返工处理。"
                        review_level = "warning"
                    post_stage_status(
                        target_stage,
                        target_type,
                        review_text,
                        conversation_id=conversation_id,
                        status_kind="review_finished",
                        status_level=review_level,
                        actor_id=f"{target_stage}-system",
                        payload={"review": review},
                    )
                    if stage_logger:
                        stage_logger(target_stage, "done", payload)
                        stage_logger(target_stage, "review", {
                            "label": stage_labels.get(target_stage, target_stage),
                            "stage_type": target_type,
                            **(payload.get("review") or {}),
                        })
                    payload["conversation_id"] = record_stage_submission(
                        target_stage,
                        target_type,
                        payload,
                        actor_id=actor_id,
                        actor_role=actor_role,
                    )
                    return payload

                def handle_stage_review_rework(target_stage: str, current_payload: Dict[str, Any]) -> Dict[str, Any] | None:
                    target_type = stage_types_by_name.get(target_stage, normalize_stage_type(target_stage))
                    target_cfg = get_stage_cfg(target_stage)
                    rework_limit = int(target_cfg.get("auto_rework_limit", 1) or 1)
                    review_blocking = _is_review_blocking(target_type, target_cfg)
                    review_rework_enabled = review_blocking or target_type in {"coding", "testing"}
                    rework_cleanup = bool(target_cfg.get("rework_cleanup", False))
                    rework_attempts = int(state.setdefault("rework_attempts", {}).get(target_stage, 0))
                    review = current_payload.get("review") or {}
                    if review.get("pass") is False and review_rework_enabled and rework_attempts < rework_limit:
                        if stage_logger:
                            stage_logger(target_stage, "rework", {
                                "label": stage_labels.get(target_stage, target_stage),
                                "stage_type": target_type,
                                "attempt": rework_attempts + 1,
                                "feedback": review.get("feedback", ""),
                                "cleanup": rework_cleanup,
                            })
                        if rework_cleanup:
                            self._cleanup_artifacts(task_obj, current_payload.get("artifacts") or [])
                            if current_payload.get("artifacts"):
                                keep = {str(a.get("uri")) for a in (current_payload.get("artifacts") or [])}
                                state["artifacts"] = [a for a in state.get("artifacts", []) if str(a.get("uri")) not in keep]
                        feedback = str(review.get("feedback") or "")
                        rework_guidance = _build_rework_guidance(target_type, feedback, attempt=rework_attempts)
                        conversation_id = str(current_payload.get("conversation_id") or collaboration.ensure_thread(
                            target_stage,
                            stage_type=target_type,
                            thread_kind="stage_loop",
                            title=f"{stage_labels.get(target_stage, target_stage)} 协作线程",
                        ))
                        reviewer_feedback = collaboration.post_message(
                            stage_name=target_stage,
                            stage_type=target_type,
                            actor_id=f"{target_stage}-reviewer",
                            actor_role="阶段评审",
                            content=(
                                f"请根据以下评审意见继续返工：{feedback}\n"
                                + ("\n要求：基于现有文件做最小增量修复。" if target_type == "coding" and not rework_cleanup else "\n要求：严格修复后重新提交完整产物。")
                                + (f"\n补充约束：\n{rework_guidance}" if rework_guidance else "")
                            ).strip(),
                            message_type="rework_request",
                            conversation_id=conversation_id,
                            recipient_id=default_actor_id(target_stage, target_type),
                            payload={**review, "rework_guidance": rework_guidance},
                        )
                        post_stage_status(
                            target_stage,
                            target_type,
                            f"{stage_labels.get(target_stage, target_stage)} 已根据评审意见进入返工。",
                            conversation_id=conversation_id,
                            status_kind="review_rework",
                            status_level="warning",
                            actor_id=f"{target_stage}-system",
                            payload={"feedback": feedback, "attempt": rework_attempts + 1},
                        )
                        collaboration.upsert_blackboard(
                            entry_key=f"stage:{target_stage}:active_rework",
                            title=f"{stage_labels.get(target_stage, target_stage)} 当前返工要求",
                            content=((feedback + ("\n\n" + rework_guidance if rework_guidance else "")).strip() or "评审未通过，需要继续修复。"),
                            entry_type="rework_request",
                            stage_name=target_stage,
                            payload={**review, "rework_guidance": rework_guidance},
                            source_message_id=reviewer_feedback.get("message_id"),
                        )
                        state.setdefault("rework_attempts", {})[target_stage] = rework_attempts + 1
                        current_payload = execute_stage_once(target_stage, reason="review_rework")
                        if current_payload is None:
                            return None
                        review = current_payload.get("review") or {}
                    if review.get("pass") is False and review_blocking:
                        err = f"stage_review_failed:{target_stage}"
                        if stage_logger:
                            stage_logger(target_stage, "error", {
                                "label": stage_labels.get(target_stage, target_stage),
                                "stage_type": target_type,
                                "error": err,
                                "feedback": review.get("feedback", ""),
                            })
                        state["error"] = err
                        return None
                    return current_payload

                def handle_coding_smoke_loop(current_payload: Dict[str, Any], coding_stage: str, reason_prefix: str = "coding") -> Dict[str, Any] | None:
                    coding_cfg = get_stage_cfg(coding_stage)
                    smoke_fix_limit = int(coding_cfg.get("auto_smoke_fix_limit", 2) or 2)
                    smoke_blocking = bool(coding_cfg.get("smoke_test_blocking", True))
                    smoke_attempts = int(state.setdefault("smoke_fix_attempts", {}).get(coding_stage, 0))
                    while self._smoke_test_failed(current_payload) and smoke_attempts < smoke_fix_limit:
                        smoke_attempts += 1
                        state.setdefault("smoke_fix_attempts", {})[coding_stage] = smoke_attempts
                        smoke_feedback = self._collect_smoke_feedback(current_payload)
                        if stage_logger:
                            stage_logger(coding_stage, "rework", {
                                "label": stage_labels.get(coding_stage, coding_stage),
                                "stage_type": stage_types_by_name.get(coding_stage, "coding"),
                                "attempt": smoke_attempts,
                                "reason": f"{reason_prefix}_smoke_failed",
                                "feedback": smoke_feedback,
                            })
                        conversation_id = str(current_payload.get("conversation_id") or collaboration.ensure_thread(
                            coding_stage,
                            stage_type="coding",
                            thread_kind="stage_loop",
                            title=f"{stage_labels.get(coding_stage, coding_stage)} 协作线程",
                        ))
                        smoke_guidance = _build_rework_guidance("coding", smoke_feedback, attempt=smoke_attempts - 1)
                        smoke_message = collaboration.post_message(
                            stage_name=coding_stage,
                            stage_type="coding",
                            actor_id=f"{coding_stage}-smoke",
                            actor_role="编码冒烟测试",
                            content=(
                                "轻量运行/冒烟校验未通过，请优先做最小增量修复。\n"
                                f"{smoke_feedback}"
                                + (f"\n补充约束：\n{smoke_guidance}" if smoke_guidance else "")
                            ).strip(),
                            message_type="smoke_feedback",
                            conversation_id=conversation_id,
                            recipient_id=default_actor_id(coding_stage, "coding"),
                            payload={"reason_prefix": reason_prefix, "feedback": smoke_feedback, "rework_guidance": smoke_guidance},
                        )
                        post_stage_status(
                            coding_stage,
                            "coding",
                            f"{stage_labels.get(coding_stage, coding_stage)} 冒烟未通过，正在进行自动修复。",
                            conversation_id=conversation_id,
                            status_kind="smoke_rework",
                            status_level="warning",
                            actor_id=f"{coding_stage}-system",
                            payload={"feedback": smoke_feedback, "attempt": smoke_attempts},
                        )
                        collaboration.upsert_blackboard(
                            entry_key=f"stage:{coding_stage}:smoke_feedback",
                            title=f"{stage_labels.get(coding_stage, coding_stage)} 冒烟反馈",
                            content=((smoke_feedback + ("\n\n" + smoke_guidance if smoke_guidance else "")).strip() or "编码阶段冒烟校验失败。"),
                            entry_type="smoke_feedback",
                            stage_name=coding_stage,
                            payload={"reason_prefix": reason_prefix, "feedback": smoke_feedback, "rework_guidance": smoke_guidance},
                            source_message_id=smoke_message.get("message_id"),
                        )
                        current_payload = execute_stage_once(coding_stage, reason="smoke_fix")
                        if current_payload is None:
                            return None
                        current_payload = handle_stage_review_rework(coding_stage, current_payload)
                        if current_payload is None:
                            return None
                    if self._smoke_test_failed(current_payload) and smoke_blocking:
                        err = "coding_smoke_failed"
                        if stage_logger:
                            stage_logger(coding_stage, "error", {
                                "label": stage_labels.get(coding_stage, coding_stage),
                                "stage_type": stage_types_by_name.get(coding_stage, "coding"),
                                "error": err,
                                "feedback": self._collect_smoke_feedback(current_payload),
                            })
                        state["error"] = err
                        return None
                    return current_payload

                payload = execute_stage_once(stage_name)
                if payload is None:
                    return state
                actual_stage = str(payload.get("stage") or stage_name)
                actual_stage_type = stage_types_by_name.get(actual_stage, normalize_stage_type(payload.get("stage_type") or actual_stage))
                human_decision = payload.get("human_decision_request") if isinstance(payload.get("human_decision_request"), dict) else None
                if human_decision:
                    task_obj.context["pending_human_decision"] = dict(human_decision)
                    state["await"] = dict(human_decision)
                    if stage_logger:
                        stage_logger(actual_stage, "await", dict(human_decision))
                    return state
                payload = handle_stage_review_rework(actual_stage, payload)
                if payload is None:
                    return state

                if current_stage_type == "coding" and actual_stage != stage_name:
                    payload = execute_stage_once(stage_name, reason="after_architecture_rework")
                    if payload is None:
                        return state
                    actual_stage = stage_name
                    actual_stage_type = current_stage_type
                    payload = handle_stage_review_rework(actual_stage, payload)
                    if payload is None:
                        return state

                if actual_stage_type == "coding":
                    payload = handle_coding_smoke_loop(payload, actual_stage)
                    if payload is None:
                        return state

                if current_stage_type == "testing":
                    testing_cfg = get_stage_cfg(stage_name)
                    test_fix_limit = int(testing_cfg.get("auto_fix_limit", 3) or 3)
                    test_fix_attempts = int(state.setdefault("test_fix_attempts", {}).get(stage_name, 0))
                    while self._testing_failed(payload) and test_fix_attempts < test_fix_limit:
                        test_fix_attempts += 1
                        state.setdefault("test_fix_attempts", {})[stage_name] = test_fix_attempts
                        test_feedback = self._collect_test_feedback(payload)
                        if stage_logger:
                            stage_logger(stage_name, "rework", {
                                "label": stage_labels.get(stage_name, stage_name),
                                "stage_type": current_stage_type,
                                "attempt": test_fix_attempts,
                                "reason": "testing_failed",
                                "feedback": test_feedback,
                            })

                        coding_stage = resolve_related_stage(stage_name, "coding", prefer_prior=True)
                        if not coding_stage:
                            state["error"] = "testing_failed_without_coding_stage"
                            return state
                        handoff_conversation = collaboration.ensure_thread(
                            coding_stage,
                            stage_type="coding",
                            thread_kind="testing_handoff",
                            peer_stage=stage_name,
                            title=f"{stage_labels.get(stage_name, stage_name)} -> {stage_labels.get(coding_stage, coding_stage)} 缺陷回传",
                            participants=[
                                {"actor_id": "tester", "role": stage_role_name(stage_name, "testing", "测试工程师")},
                                {"actor_id": default_actor_id(coding_stage, "coding"), "role": stage_role_name(coding_stage, "coding", "软件工程师")},
                            ],
                        )
                        testing_feedback_message = collaboration.post_message(
                            stage_name=coding_stage,
                            stage_type="coding",
                            actor_id="tester",
                            actor_role=stage_role_name(stage_name, "testing", "测试工程师"),
                            content=(
                                "全面测试阶段发现缺陷，请先修复后再回到全面测试。\n"
                                f"{test_feedback}"
                            ).strip(),
                            message_type="test_feedback",
                            conversation_id=handoff_conversation,
                            thread_kind="testing_handoff",
                            recipient_id=default_actor_id(coding_stage, "coding"),
                            payload={"source_stage": stage_name, "feedback": test_feedback},
                        )
                        collaboration.upsert_blackboard(
                            entry_key=f"stage:{coding_stage}:test_feedback",
                            title=f"{stage_labels.get(coding_stage, coding_stage)} 最新测试反馈",
                            content=test_feedback or "全面测试阶段发现缺陷。",
                            entry_type="test_feedback",
                            stage_name=coding_stage,
                            payload={"source_stage": stage_name, "feedback": test_feedback},
                            source_message_id=testing_feedback_message.get("message_id"),
                        )

                        coding_payload = execute_stage_once(coding_stage, reason="fix_from_testing")
                        if coding_payload is None:
                            return state
                        coding_payload = handle_stage_review_rework(coding_stage, coding_payload)
                        if coding_payload is None:
                            return state
                        coding_payload = handle_coding_smoke_loop(coding_payload, coding_stage, reason_prefix="fix_from_testing")
                        if coding_payload is None:
                            return state

                        payload = execute_stage_once(stage_name, reason="after_code_fix")
                        if payload is None:
                            return state
                        payload = handle_stage_review_rework(stage_name, payload)
                        if payload is None:
                            return state

                    if self._testing_failed(payload):
                        err = "testing_failed_after_rework"
                        if stage_logger:
                            stage_logger(stage_name, "error", {
                                "label": stage_labels.get(stage_name, stage_name),
                                "stage_type": current_stage_type,
                                "error": err,
                                "feedback": self._collect_test_feedback(payload),
                            })
                        state["error"] = err
                        return state
                state.pop("resume", None)
                if not state.get("await"):
                    state.pop("await", None)
                return state

            return node

        first = None
        ordered_names: List[str] = []
        for st in stages:
            name = str(st["name"])
            human_flag = bool(st.get("human_checkpoint", False))
            sg.add_node(name, make_node(name, human_flag))
            ordered_names.append(name)
            if not first:
                first = name

        for idx, name in enumerate(ordered_names):
            next_name = ordered_names[idx + 1] if idx + 1 < len(ordered_names) else END

            def route(state: Dict[str, Any], default_next=next_name):
                if state.get("error") or state.get("await") or state.get("abort"):
                    return END
                return default_next

            sg.add_conditional_edges(name, route, {END: END, next_name: next_name})
        sg.set_entry_point(first)
        return sg.compile()

def init_task_workspace(base_dir: str, task: Task):
    root = os.path.abspath(task.workspace_path or os.path.join(base_dir, task.task_id))
    for sub in ["analysis", "design", "code", "tests", "docs", "logs", "patches", "plan"]:
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root
