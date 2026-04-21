"""单阶段执行服务，负责运行 Agent、应用能力并记录评审结果。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from core import AgentMessage, SystemState
from orchestration.capabilities.invoker import (
    build_requested_capability_execution,
    extract_capability_invocations,
)
from orchestration.planning.workspace_cleanup import cleanup_architecture_orphan_files


Payload = Dict[str, Any]


@dataclass
class StageExecutionContext:
    task: Any
    state: Dict[str, Any]
    base_dir: str
    stage_labels: Dict[str, str]
    stage_types_by_name: Dict[str, str]
    stage_caps: Dict[str, List[str]]
    collaboration: Any
    capability_runtime: Any
    storage: Any
    stage_logger: Optional[Callable[[str, str, Dict[str, Any]], None]]
    should_abort: Optional[Callable[[Any], bool]]
    create_agent: Callable[[str], Any]
    get_stage_cfg: Callable[[str], Dict[str, Any]]
    default_actor_id: Callable[[str, str], str]
    stage_role_name: Callable[[str, str, str], str]
    apply_runtime_collaboration_context: Callable[[str], None]
    clear_runtime_collaboration_context: Callable[[], None]
    post_stage_status: Callable[..., str]
    record_stage_submission: Callable[..., str]
    write_text: Callable[[str, str, str, str], str]
    write_runtime_artifact: Callable[[Dict[str, Any]], Dict[str, Any]]
    review_stage_output: Callable[[Any, str, Dict[str, Any], Optional[str], Optional[Callable[[Dict[str, Any]], None]]], Dict[str, Any]]
    resolve_related_stage: Callable[[str, str], Optional[str]]
    extract_human_decision_request: Callable[[str, str, str, Dict[str, Any]], Optional[Dict[str, Any]]]
    cleanup_architecture_orphans: Callable[[Any, str, str, Optional[Dict[str, Any]]], int] = lambda *_args, **_kwargs: 0


class StageExecutionService:
    def __init__(self, ctx: StageExecutionContext):
        self.ctx = ctx

    def _to_payload(self, exec_result: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if isinstance(exec_result, AgentMessage):
            for art in exec_result.artifacts:
                if art.get("uri") is None and art.get("content"):
                    fname = art.get("filename") or "artifact.txt"
                    path = self.ctx.write_text(self.ctx.base_dir, self.ctx.task.task_id, fname, art["content"])
                    art["uri"] = path
                self.ctx.state.setdefault("artifacts", []).append(art)
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
            path = self.ctx.write_text(self.ctx.base_dir, self.ctx.task.task_id, exec_result["filename"], exec_result["content"])
            stage_artifact = {"uri": path, "type": exec_result["type"]}
            stage_artifacts.append(stage_artifact)
            self.ctx.state.setdefault("artifacts", []).append(stage_artifact)
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

    def _handle_prerequisite_error(
        self,
        target_stage: str,
        target_type: str,
        actor_id: str,
        actor_role: str,
        err_text: str,
    ) -> Optional[Payload]:
        architecture_stage = self.ctx.resolve_related_stage(target_stage, "architecture")
        if not architecture_stage:
            if self.ctx.stage_logger:
                self.ctx.stage_logger(target_stage, "error", {"error": err_text, "label": self.ctx.stage_labels.get(target_stage, target_stage), "stage_type": target_type})
            self.ctx.state["error"] = err_text
            return None
        arch_attempts = int(self.ctx.state.setdefault("arch_rework_attempts", {}).get(architecture_stage, 0)) + 1
        self.ctx.state.setdefault("arch_rework_attempts", {})[architecture_stage] = arch_attempts
        if self.ctx.stage_logger:
            self.ctx.stage_logger(architecture_stage, "rework", {
                "label": self.ctx.stage_labels.get(architecture_stage, architecture_stage),
                "stage_type": self.ctx.stage_types_by_name.get(architecture_stage, "architecture"),
                "attempt": arch_attempts,
                "reason": "coding_prerequisite_missing",
                "feedback": err_text,
            })
        architecture_conversation = self.ctx.collaboration.ensure_thread(
            architecture_stage,
            stage_type="architecture",
            thread_kind="prerequisite_rework",
            peer_stage=target_stage,
            title=f"{self.ctx.stage_labels.get(architecture_stage, architecture_stage)} 前置条件修复",
            participants=[
                {"actor_id": actor_id, "role": actor_role},
                {"actor_id": "architect", "role": self.ctx.stage_role_name(architecture_stage, "architecture", "架构设计师")},
            ],
        )
        feedback_message = self.ctx.collaboration.post_message(
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
        self.ctx.collaboration.upsert_blackboard(
            entry_key=f"stage:{architecture_stage}:prerequisite_gap",
            title=f"{self.ctx.stage_labels.get(architecture_stage, architecture_stage)} 前置缺口",
            content=f"{self.ctx.stage_labels.get(target_stage, target_stage)} 反馈：{err_text}",
            entry_type="prerequisite_gap",
            stage_name=architecture_stage,
            payload={"source_stage": target_stage, "error": err_text},
            source_message_id=feedback_message.get("message_id"),
        )
        self.ctx.post_stage_status(
            architecture_stage,
            "architecture",
            f"{self.ctx.stage_labels.get(target_stage, target_stage)} 发现前置条件缺口，已转交架构阶段返工。",
            conversation_id=architecture_conversation,
            status_kind="prerequisite_rework",
            status_level="warning",
            actor_id=f"{architecture_stage}-system",
            payload={"source_stage": target_stage, "error": err_text},
        )
        return self.execute_once(architecture_stage, reason=f"{target_stage}_prerequisite_rework")

    def execute_once(self, target_stage: str, reason: str | None = None) -> Optional[Payload]:
        target_type = self.ctx.stage_types_by_name.get(target_stage, target_stage)
        target_cfg = self.ctx.get_stage_cfg(target_stage)
        actor_id = self.ctx.default_actor_id(target_stage, target_type)
        actor_role = self.ctx.stage_role_name(target_stage, target_type, fallback=target_type)
        if target_type == "coding" and reason in {"review_rework", "smoke_fix", "fix_from_testing", "after_architecture_rework"}:
            orphan_removed = self.ctx.cleanup_architecture_orphans(
                self.ctx.task,
                self.ctx.base_dir,
                target_stage,
                {"stage_type": target_type},
            )
            if orphan_removed and self.ctx.stage_logger:
                self.ctx.stage_logger(target_stage, "progress", {
                    "label": self.ctx.stage_labels.get(target_stage, target_stage),
                    "stage_type": target_type,
                    "progress_kind": "cleanup",
                    "progress_state": "done",
                    "reason": reason,
                    "removed_orphans": orphan_removed,
                    "message": f"已清理 {orphan_removed} 个架构清单外的废弃文件",
                })
        if self.ctx.should_abort and self.ctx.should_abort(self.ctx.task):
            self.ctx.state["abort"] = {"stage": target_stage, "stage_type": target_type, "reason": "task_aborted"}
            if self.ctx.stage_logger:
                self.ctx.stage_logger(target_stage, "abort", {"label": self.ctx.stage_labels.get(target_stage, target_stage), "stage_type": target_type, "reason": "task_aborted"})
            return None
        self.ctx.apply_runtime_collaboration_context(target_stage)
        conversation_id = None
        capability_directives: List[Dict[str, Any]] = []
        try:
            agent = self.ctx.create_agent(target_stage)
            actor_id = getattr(agent, "id", actor_id)
            actor_role = getattr(agent, "role_name", actor_role) or actor_role
            conversation_id = self.ctx.post_stage_status(
                target_stage,
                target_type,
                f"{self.ctx.stage_labels.get(target_stage, target_stage)} 已交给 {actor_role}，等待模型返回。",
                status_kind="agent_waiting",
                status_level="waiting",
                actor_id=f"{target_stage}-system",
                payload={"reason": reason or "", "planned_role": actor_role},
                recipient_id=actor_id,
            )
            if self.ctx.stage_logger:
                start_payload = {"label": self.ctx.stage_labels.get(target_stage, target_stage), "stage_type": target_type}
                if reason:
                    start_payload["reason"] = reason
                self.ctx.stage_logger(target_stage, "start", start_payload)
            exec_result = agent.act(self.ctx.task, SystemState())
            exec_result, capability_directives = extract_capability_invocations(exec_result)
            conversation_id = self.ctx.post_stage_status(
                target_stage,
                target_type,
                f"{self.ctx.stage_labels.get(target_stage, target_stage)} 已收到模型返回，正在整理产物与上下文。",
                conversation_id=conversation_id,
                status_kind="agent_returned",
                status_level="active",
                actor_id=f"{target_stage}-system",
                payload={"planned_role": actor_role},
            )
            if capability_directives and self.ctx.stage_logger:
                self.ctx.stage_logger(target_stage, "progress", {
                    "label": self.ctx.stage_labels.get(target_stage, target_stage),
                    "stage_type": target_type,
                    "progress_kind": "capability_invoke",
                    "progress_state": "detected",
                    "count": len(capability_directives),
                    "message": f"检测到 {len(capability_directives)} 个能力调用请求",
                })
        except Exception as exc:
            self.ctx.clear_runtime_collaboration_context()
            err_text = str(exc)
            if conversation_id:
                self.ctx.post_stage_status(
                    target_stage,
                    target_type,
                    f"{self.ctx.stage_labels.get(target_stage, target_stage)} 执行失败：{err_text}",
                    conversation_id=conversation_id,
                    status_kind="agent_error",
                    status_level="error",
                    actor_id=f"{target_stage}-system",
                    payload={"error": err_text},
                )
            if target_type == "coding" and err_text.startswith(("architecture_missing_doc:", "architecture_missing_file_list_section:", "architecture_invalid_file_list:")):
                return self._handle_prerequisite_error(target_stage, target_type, actor_id, actor_role, err_text)
            if self.ctx.stage_logger:
                self.ctx.stage_logger(target_stage, "error", {"error": err_text, "label": self.ctx.stage_labels.get(target_stage, target_stage), "stage_type": target_type})
            self.ctx.state["error"] = err_text
            return None
        finally:
            self.ctx.clear_runtime_collaboration_context()

        payload = self._to_payload(exec_result)
        payload["stage"] = target_stage
        payload["stage_type"] = target_type
        payload["label"] = self.ctx.stage_labels.get(target_stage, target_stage)
        if target_cfg.get("planned_role"):
            payload["planned_role"] = target_cfg.get("planned_role")
        if target_cfg.get("acceptance_criteria"):
            payload["acceptance_criteria"] = target_cfg.get("acceptance_criteria")
        requested_capabilities, requested_capability_options = build_requested_capability_execution(capability_directives)
        if capability_directives:
            payload["requested_capability_invocations"] = capability_directives
        capability_progress = None
        if self.ctx.stage_logger:
            capability_progress = lambda cap_payload: self.ctx.stage_logger(target_stage, "progress", {
                "label": self.ctx.stage_labels.get(target_stage, target_stage),
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
            payload = self.ctx.capability_runtime.apply_stage_capabilities(
                task=self.ctx.task,
                state=self.ctx.state,
                stage_name=target_stage,
                stage_type=target_type,
                stage_label=self.ctx.stage_labels.get(target_stage, target_stage),
                stage_config=request_stage_cfg,
                capabilities=requested_capabilities,
                exec_result=exec_result,
                payload=payload,
                write_artifact=self.ctx.write_runtime_artifact,
                progress_callback=capability_progress,
            )
            executed_capability_ids.update(requested_capabilities)
        remaining_capabilities = [
            capability_id for capability_id in (self.ctx.stage_caps.get(target_stage, []) or [])
            if str(capability_id or "").strip() and str(capability_id or "").strip() not in executed_capability_ids
        ]
        payload = self.ctx.capability_runtime.apply_stage_capabilities(
            task=self.ctx.task,
            state=self.ctx.state,
            stage_name=target_stage,
            stage_type=target_type,
            stage_label=self.ctx.stage_labels.get(target_stage, target_stage),
            stage_config=target_cfg,
            capabilities=remaining_capabilities,
            exec_result=exec_result,
            payload=payload,
            write_artifact=self.ctx.write_runtime_artifact,
            progress_callback=capability_progress,
        )
        review_progress = None
        if self.ctx.stage_logger:
            review_progress = lambda review_payload: self.ctx.stage_logger(target_stage, "progress", {
                "label": self.ctx.stage_labels.get(target_stage, target_stage),
                "stage_type": target_type,
                **(review_payload or {}),
            })
        conversation_id = self.ctx.post_stage_status(
            target_stage,
            target_type,
            f"{self.ctx.stage_labels.get(target_stage, target_stage)} 产物已生成，正在发起阶段评审。",
            conversation_id=conversation_id,
            status_kind="review_started",
            status_level="active",
            actor_id=f"{target_stage}-system",
        )
        payload["review"] = self.ctx.review_stage_output(self.ctx.task, target_stage, payload, target_type, review_progress)
        payload["human_decision_request"] = self.ctx.extract_human_decision_request(
            target_stage,
            target_type,
            self.ctx.stage_labels.get(target_stage, target_stage),
            payload.get("review") or {},
        )
        review = payload.get("review") or {}
        review_text = "评审已完成。"
        review_level = "done"
        if isinstance(review, dict) and review.get("review_status") == "skipped":
            review_text = f"{self.ctx.stage_labels.get(target_stage, target_stage)} 跳过了阶段评审。"
            review_level = "active"
        elif isinstance(review, dict) and review.get("pass") is True:
            review_text = f"{self.ctx.stage_labels.get(target_stage, target_stage)} 评审通过，准备进入下一步。"
            review_level = "done"
        elif isinstance(review, dict) and review.get("pass") is False:
            review_text = f"{self.ctx.stage_labels.get(target_stage, target_stage)} 评审未通过，等待返工处理。"
            review_level = "warning"
        self.ctx.post_stage_status(
            target_stage,
            target_type,
            review_text,
            conversation_id=conversation_id,
            status_kind="review_finished",
            status_level=review_level,
            actor_id=f"{target_stage}-system",
            payload={"review": review},
        )
        if self.ctx.stage_logger:
            self.ctx.stage_logger(target_stage, "done", payload)
            self.ctx.stage_logger(target_stage, "review", {
                "label": self.ctx.stage_labels.get(target_stage, target_stage),
                "stage_type": target_type,
                **(payload.get("review") or {}),
            })
        payload["conversation_id"] = self.ctx.record_stage_submission(
            target_stage,
            target_type,
            payload,
            actor_id=actor_id,
            actor_role=actor_role,
        )
        return payload
