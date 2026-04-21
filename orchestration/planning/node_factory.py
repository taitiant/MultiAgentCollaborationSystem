"""阶段节点工厂，负责组装节点执行上下文并生成 LangGraph 节点函数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from core import Task
from orchestration.collab import CollaborationHub
from orchestration.execution.stage_agent_registry import StageAgentRequest
from orchestration.execution.stage_execution import StageExecutionContext, StageExecutionService
from orchestration.execution.workflow_policy import (
    WorkflowPolicyContext,
    apply_coding_smoke_loop,
    apply_review_rework,
    apply_testing_fix_loop,
)
from orchestration.planning.stage_catalog import normalize_stage_type


@dataclass
class GraphNodeFactory:
    task: Task
    base_dir: str
    storage: Any
    stage_logger: Optional[Callable[[str, str, Dict[str, Any]], None]]
    should_abort: Optional[Callable[[Task], bool]]
    create_agent_fn: Callable[[str], Any]
    get_stage_cfg: Callable[[str], Dict[str, Any]]
    resolve_related_stage: Callable[[str, str], Optional[str]]
    stage_labels: Dict[str, str]
    stage_types_by_name: Dict[str, str]
    stage_defs_by_name: Dict[str, Dict[str, Any]]
    stage_skills: Dict[str, List[str]]
    stage_caps: Dict[str, List[str]]
    capability_index: Dict[str, Dict[str, Any]]
    skill_settings: Dict[str, Any]
    capability_runtime: Any
    build_skill_runtime_context: Callable[[List[str], Dict[str, Any]], str]
    build_capability_invoke_prompt: Callable[[List[Dict[str, Any]]], str]
    review_stage_output: Callable[..., Dict[str, Any]]
    cleanup_artifacts: Callable[[Task, List[Dict[str, Any]]], None]
    smoke_test_failed: Callable[[Dict[str, Any]], bool]
    testing_failed: Callable[[Dict[str, Any]], bool]
    collect_smoke_feedback: Callable[[Dict[str, Any]], str]
    collect_test_feedback: Callable[[Dict[str, Any]], str]
    build_rework_guidance: Callable[[str, str, int], str]
    is_review_blocking: Callable[[str, Dict[str, Any]], bool]
    write_text: Callable[[str, str, str, str], str]
    extract_human_decision_request: Callable[[str, str, str, Dict[str, Any]], Optional[Dict[str, Any]]]
    cleanup_architecture_orphans: Callable[[Any, str, str, Optional[Dict[str, Any]]], int]

    def _stage_invokable_capabilities(self, stage_name: str) -> List[Dict[str, Any]]:
        resolved: List[Dict[str, Any]] = []
        seen = set()
        stage_type = self.stage_types_by_name.get(stage_name, normalize_stage_type(stage_name))
        candidate_ids = list(self.stage_caps.get(stage_name, []) or [])
        for capability_id, capability_def in self.capability_index.items():
            if capability_id in seen:
                continue
            recommended = capability_def.get("recommended_stage_types") or []
            if capability_def.get("enabled", True) and capability_def.get("planner_visible", True) and stage_type in recommended:
                candidate_ids.append(capability_id)
        for capability_id in candidate_ids:
            normalized_id = str(capability_id or "").strip()
            if not normalized_id or normalized_id in seen:
                continue
            capability_def = self.capability_index.get(normalized_id)
            if not isinstance(capability_def, dict):
                continue
            if capability_def.get("enabled", True) is False:
                continue
            seen.add(normalized_id)
            resolved.append(dict(capability_def))
        return resolved

    def _default_actor_id(self, target_stage: str, target_type: str) -> str:
        mapping = {
            "requirements": "req-analyst",
            "architecture": "architect",
            "assets": "asset-designer",
            "coding": "patcher",
            "testing": "tester",
            "docs": "doc-writer",
        }
        return mapping.get(target_type, target_stage)

    def _stage_role_name(self, target_stage: str, target_type: str, fallback: str = "") -> str:
        stage_cfg = self.get_stage_cfg(target_stage)
        stage_def = self.stage_defs_by_name.get(target_stage, {})
        return str(stage_cfg.get("planned_role") or stage_def.get("role") or fallback or target_type)

    def _apply_runtime_collaboration_context(self, task_obj: Task, collaboration: CollaborationHub, target_stage: str) -> None:
        prompt_context = collaboration.build_stage_prompt_context(target_stage)
        skill_context = self.build_skill_runtime_context(self.stage_skills.get(target_stage, []), self.skill_settings)
        capability_context = self.build_capability_invoke_prompt(self._stage_invokable_capabilities(target_stage))
        prompt_context = "\n\n".join(
            part for part in [prompt_context, skill_context, capability_context] if str(part or "").strip()
        )
        task_obj.context["_runtime_collaboration"] = {
            "stage_name": target_stage,
            "prompt_context": prompt_context,
            "selection_context": collaboration.build_stage_targeted_context(target_stage),
        }

    def _clear_runtime_collaboration_context(self, task_obj: Task) -> None:
        task_obj.context.pop("_runtime_collaboration", None)

    def _record_stage_submission(
        self,
        collaboration: CollaborationHub,
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
            title=f"{self.stage_labels.get(target_stage, target_stage)} 协作线程",
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
            title=f"{self.stage_labels.get(target_stage, target_stage)} 最新交付",
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
                title=f"{self.stage_labels.get(target_stage, target_stage)} 最新评审",
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
                    self.stage_labels.get(target_stage, target_stage),
                    payload,
                    review,
                )
                if review.get("pass") is True
                else f"{self.stage_labels.get(target_stage, target_stage)} 评审未通过，原结论暂不固化为长期记忆。"
            )
            collaboration.upsert_blackboard(
                entry_key=f"stage:{target_stage}:decision_memory",
                title=f"{self.stage_labels.get(target_stage, target_stage)} 决策记忆",
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
                    title=f"{self.stage_labels.get(target_stage, target_stage)} 待人工决策",
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

    def _post_stage_status(
        self,
        collaboration: CollaborationHub,
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
            title=f"{self.stage_labels.get(target_stage, target_stage)} 协作线程",
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

    def _write_runtime_artifact(self, task_obj: Task, state: Dict[str, Any], artifact: Dict[str, Any]) -> Dict[str, Any]:
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
                uri = self.write_text(self.base_dir, task_obj.task_id, filename, str(content))
        else:
            return normalized
        normalized["uri"] = uri
        normalized.pop("data", None)
        state.setdefault("artifacts", []).append(normalized)
        return normalized

    def make_node(self, stage_name: str, human_checkpoint: bool):
        current_stage_type = self.stage_types_by_name.get(stage_name, normalize_stage_type(stage_name))

        def node(state: Dict[str, Any]):
            if state.get("error"):
                return state
            if self.should_abort and self.should_abort(state["task"]):
                state["abort"] = {"stage": stage_name, "stage_type": current_stage_type, "reason": "task_aborted"}
                if self.stage_logger:
                    self.stage_logger(stage_name, "abort", {"label": self.stage_labels.get(stage_name, stage_name), "stage_type": current_stage_type, "reason": "task_aborted"})
                return state
            if human_checkpoint and not state.get("resume", False):
                state["await"] = {"stage": stage_name, "stage_type": current_stage_type, "label": self.stage_labels.get(stage_name, stage_name)}
                if self.stage_logger:
                    self.stage_logger(stage_name, "await", {"label": self.stage_labels.get(stage_name, stage_name), "stage_type": current_stage_type})
                return state
            task_obj: Task = state["task"]
            collaboration = CollaborationHub(task_obj)

            def create_agent(target_stage: str):
                stage_def = self.stage_defs_by_name.get(target_stage, {"name": target_stage, "stage_type": normalize_stage_type(target_stage)})
                stage_type = self.stage_types_by_name.get(target_stage, normalize_stage_type(stage_def.get("stage_type") or target_stage))
                return self.create_agent_fn(target_stage)

            stage_execution_service = StageExecutionService(
                StageExecutionContext(
                    task=task_obj,
                    state=state,
                    base_dir=self.base_dir,
                    stage_labels=self.stage_labels,
                    stage_types_by_name=self.stage_types_by_name,
                    stage_caps=self.stage_caps,
                    collaboration=collaboration,
                    capability_runtime=self.capability_runtime,
                    storage=self.storage,
                    stage_logger=self.stage_logger,
                    should_abort=self.should_abort,
                    create_agent=create_agent,
                    get_stage_cfg=self.get_stage_cfg,
                    default_actor_id=self._default_actor_id,
                    stage_role_name=self._stage_role_name,
                    apply_runtime_collaboration_context=lambda target_stage: self._apply_runtime_collaboration_context(task_obj, collaboration, target_stage),
                    clear_runtime_collaboration_context=lambda: self._clear_runtime_collaboration_context(task_obj),
                    post_stage_status=lambda *args, **kwargs: self._post_stage_status(collaboration, *args, **kwargs),
                    record_stage_submission=lambda *args, **kwargs: self._record_stage_submission(collaboration, *args, **kwargs),
                    write_text=self.write_text,
                    write_runtime_artifact=lambda artifact: self._write_runtime_artifact(task_obj, state, artifact),
                    review_stage_output=self.review_stage_output,
                    resolve_related_stage=lambda target_stage, stage_type: self.resolve_related_stage(target_stage, stage_type),
                    extract_human_decision_request=self.extract_human_decision_request,
                    cleanup_architecture_orphans=self.cleanup_architecture_orphans,
                )
            )

            def execute_stage_once(target_stage: str, reason: str | None = None) -> Dict[str, Any] | None:
                return stage_execution_service.execute_once(target_stage, reason=reason)

            policy_ctx = WorkflowPolicyContext(
                state=state,
                collaboration=collaboration,
                execute_stage_once=execute_stage_once,
                post_stage_status=lambda *args, **kwargs: self._post_stage_status(collaboration, *args, **kwargs),
                get_stage_cfg=self.get_stage_cfg,
                resolve_related_stage=lambda target_stage, stage_type: self.resolve_related_stage(target_stage, stage_type),
                default_actor_id=self._default_actor_id,
                stage_role_name=self._stage_role_name,
                stage_logger=self.stage_logger,
                cleanup_artifacts=self.cleanup_artifacts,
                smoke_failed=self.smoke_test_failed,
                testing_failed=self.testing_failed,
                collect_smoke_feedback=self.collect_smoke_feedback,
                collect_test_feedback=self.collect_test_feedback,
                is_review_blocking=self.is_review_blocking,
                build_rework_guidance=self.build_rework_guidance,
                task=task_obj,
                stage_labels=self.stage_labels,
                stage_types_by_name=self.stage_types_by_name,
            )

            payload = execute_stage_once(stage_name)
            if payload is None:
                return state
            actual_stage = str(payload.get("stage") or stage_name)
            actual_stage_type = self.stage_types_by_name.get(actual_stage, normalize_stage_type(payload.get("stage_type") or actual_stage))
            human_decision = payload.get("human_decision_request") if isinstance(payload.get("human_decision_request"), dict) else None
            if human_decision:
                task_obj.context["pending_human_decision"] = dict(human_decision)
                state["await"] = dict(human_decision)
                if self.stage_logger:
                    self.stage_logger(actual_stage, "await", dict(human_decision))
                return state
            payload = apply_review_rework(policy_ctx, actual_stage, payload)
            if payload is None:
                return state

            if current_stage_type == "coding" and actual_stage != stage_name:
                payload = execute_stage_once(stage_name, reason="after_architecture_rework")
                if payload is None:
                    return state
                actual_stage = stage_name
                actual_stage_type = current_stage_type
                payload = apply_review_rework(policy_ctx, actual_stage, payload)
                if payload is None:
                    return state

            if actual_stage_type == "coding":
                payload = apply_coding_smoke_loop(policy_ctx, payload, actual_stage)
                if payload is None:
                    return state

            if current_stage_type == "testing":
                payload = apply_testing_fix_loop(policy_ctx, stage_name, current_stage_type, payload)
                if payload is None:
                    return state
            state.pop("resume", None)
            if not state.get("await"):
                state.pop("await", None)
            return state

        return node
