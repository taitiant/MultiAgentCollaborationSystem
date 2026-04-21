"""软件交付运行时的工作流图构建器与阶段编排入口。"""

from __future__ import annotations
import os
import yaml
import json
import re
import shutil
from typing import Dict, Any, List, Callable, Optional
from langgraph.graph import StateGraph
from langgraph.graph import END

from core import Task, SystemState, new_event
from adapters.model_registry import ModelRegistry
from domains.software_dev.agents.document_agents import ArchAgent, DocAgent, ReqAgent
from orchestration.capabilities.invoker import (
    build_capability_invoke_prompt,
)
from orchestration.capabilities.registry import capability_prompt_view, merge_capability_settings
from orchestration.capabilities.runtime import CapabilityRuntime
from orchestration.collab import CollaborationHub
from orchestration.planning.document_rules import (
    _architecture_validation_issues,
    _docs_validation_issues,
    _extract_architecture_file_list,
    _extract_file_paths_from_lines,
    _infer_declared_stack,
    _infer_project_stack,
    _normalize_architecture_file_list,
    _normalize_architecture_markdown,
)
from orchestration.mcp.registry import merge_mcp_settings
from orchestration.file_utils import write_text, ensure_workspace
from orchestration.planning.stage_catalog import (
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
from orchestration.skills.registry import (
    build_skill_runtime_context,
    merge_skill_settings,
    skill_prompt_view,
)
from orchestration.planning.workflow_plan import (
    _build_fallback_plan,
    _make_stage_instance,
    _normalize_stage_plan,
    resolve_conversation_groups,
    write_leader_plan_snapshot,
)
from orchestration.execution.stage_agent_registry import (
    StageAgentRequest,
    StageAgentRegistry,
    build_default_stage_agent_registry,
)
from orchestration.execution.stage_execution import StageExecutionContext, StageExecutionService
from orchestration.execution.workflow_policy import (
    WorkflowPolicyContext,
    apply_coding_smoke_loop,
    apply_review_rework,
    apply_testing_fix_loop,
)
from orchestration.planning.designer import WorkflowDesigner
from orchestration.planning.model_selection import ModelSelector
from orchestration.planning.node_factory import GraphNodeFactory
from orchestration.planning.reviewer import WorkflowStageReviewer
from orchestration.planning.workspace_cleanup import cleanup_architecture_orphan_files
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

class GraphBuilder:
    def __init__(
        self,
        base_dir: str,
        model_registry: ModelRegistry,
        storage=None,
        capability_settings_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        skill_settings_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        mcp_settings_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        stage_agent_registry: Optional[StageAgentRegistry] = None,
    ):
        self.base_dir = base_dir
        self.model_registry = model_registry
        self.storage = storage or FileStore()
        self.capability_settings_provider = capability_settings_provider
        self.skill_settings_provider = skill_settings_provider
        self.mcp_settings_provider = mcp_settings_provider
        self.stage_agent_registry = stage_agent_registry or build_default_stage_agent_registry()
        self.model_selector = ModelSelector(self.model_registry)
        self.workflow_designer = WorkflowDesigner(
            base_dir=self.base_dir,
            select_model=lambda task, stage_name=None, capabilities=None: self._select_model(task, stage_name=stage_name, capabilities=capabilities),
        )
        self.stage_reviewer = WorkflowStageReviewer(
            base_dir=self.base_dir,
            select_model=lambda task, stage_name=None, capabilities=None: self._select_model(task, stage_name=stage_name, capabilities=capabilities),
            emit_stage_progress=_emit_stage_progress,
            load_task_artifact_text=_load_task_artifact_text,
            extract_agent_decision_candidates=_extract_agent_decision_candidates,
            artifact_command_failed=self._artifact_command_failed,
        )

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
        # Leader 规划阶段：根据任务动态设计执行流程。
        capability_settings = self._get_capability_settings()
        skill_settings = self._get_skill_settings()
        return self.workflow_designer.plan(
            task=task,
            template=template,
            capability_settings=capability_settings,
            skill_settings=skill_settings,
        )

    def load_template(self, template_path: str) -> Dict[str, Any]:
        with open(template_path, "r") as f:
            return json.load(f)

    def load_agents(self, agents_yaml: str) -> Dict[str, Dict[str, Any]]:
        with open(agents_yaml, "r") as f:
            data = yaml.safe_load(f)
        return {a["id"]: a for a in data.get("agents", [])}

    def _select_model(self, task: Task, stage_name: str | None = None, capabilities: List[str] | None = None):
        return self.model_selector.select(task, stage_name=stage_name, capabilities=capabilities)

    def _review_stage_output(self, task: Task, stage_name: str, payload: Dict[str, Any], stage_type: str | None = None, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        return self.stage_reviewer.review(
            task=task,
            stage_name=stage_name,
            payload=payload,
            stage_type=stage_type,
            progress_callback=progress_callback,
        )

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
        return self._artifact_command_failed(payload, {"test_result", "compile_result", "startup_smoke_result"})

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
        """根据动态阶段规划构建 LangGraph 工作流。"""
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
            return self.stage_agent_registry.create(
                StageAgentRequest(
                    stage_name=stage_name,
                    stage_type=stage_type,
                    prompt_template=prompt_template,
                    model_adapter=model_adapter,
                    progress_callback=progress_callback,
                )
            )

        node_factory = GraphNodeFactory(
            task=task,
            base_dir=self.base_dir,
            storage=self.storage,
            stage_logger=stage_logger,
            should_abort=should_abort,
            create_agent_fn=create_agent,
            get_stage_cfg=get_stage_cfg,
            resolve_related_stage=lambda target_stage, stage_type: resolve_related_stage(target_stage, stage_type, prefer_prior=True),
            stage_labels=stage_labels,
            stage_types_by_name=stage_types_by_name,
            stage_defs_by_name=stage_defs_by_name,
            stage_skills=stage_skills,
            stage_caps=stage_caps,
            capability_index=capability_index,
            skill_settings=skill_settings,
            capability_runtime=capability_runtime,
            build_skill_runtime_context=build_skill_runtime_context,
            build_capability_invoke_prompt=build_capability_invoke_prompt,
            review_stage_output=lambda task, stage_name, payload, stage_type, progress_callback: self._review_stage_output(
                task, stage_name, payload, stage_type=stage_type, progress_callback=progress_callback
            ),
            cleanup_artifacts=self._cleanup_artifacts,
            smoke_test_failed=self._smoke_test_failed,
            testing_failed=self._testing_failed,
            collect_smoke_feedback=self._collect_smoke_feedback,
            collect_test_feedback=self._collect_test_feedback,
            build_rework_guidance=lambda stage_type, feedback, attempt: _build_rework_guidance(stage_type, feedback, attempt=attempt),
            is_review_blocking=_is_review_blocking,
            write_text=write_text,
            extract_human_decision_request=_extract_human_decision_request,
            cleanup_architecture_orphans=cleanup_architecture_orphan_files,
        )

        first = None
        ordered_names: List[str] = []
        for st in stages:
            name = str(st["name"])
            human_flag = bool(st.get("human_checkpoint", False))
            sg.add_node(name, node_factory.make_node(name, human_flag))
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
