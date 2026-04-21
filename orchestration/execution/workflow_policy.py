"""返工与重试策略，处理评审失败、冒烟修复与测试回传闭环。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


Payload = Dict[str, Any]


@dataclass
class WorkflowPolicyContext:
    state: Dict[str, Any]
    collaboration: Any
    execute_stage_once: Callable[[str, Optional[str]], Optional[Payload]]
    post_stage_status: Callable[..., str]
    get_stage_cfg: Callable[[str], Dict[str, Any]]
    resolve_related_stage: Callable[[str, str], Optional[str]]
    default_actor_id: Callable[[str, str], str]
    stage_role_name: Callable[[str, str, str], str]
    stage_logger: Optional[Callable[[str, str, Dict[str, Any]], None]]
    cleanup_artifacts: Callable[[Any, list[Dict[str, Any]]], None]
    smoke_failed: Callable[[Payload], bool]
    testing_failed: Callable[[Payload], bool]
    collect_smoke_feedback: Callable[[Payload], str]
    collect_test_feedback: Callable[[Payload], str]
    is_review_blocking: Callable[[str, Dict[str, Any]], bool]
    build_rework_guidance: Callable[[str, str, int], str]
    task: Any
    stage_labels: Dict[str, str]
    stage_types_by_name: Dict[str, str]


def apply_review_rework(
    ctx: WorkflowPolicyContext,
    target_stage: str,
    current_payload: Payload,
) -> Optional[Payload]:
    target_type = ctx.stage_types_by_name.get(target_stage, target_stage)
    target_cfg = ctx.get_stage_cfg(target_stage)
    rework_limit = int(target_cfg.get("auto_rework_limit", 1) or 1)
    review_blocking = ctx.is_review_blocking(target_type, target_cfg)
    review_rework_enabled = review_blocking or target_type in {"coding", "testing"}
    rework_cleanup = bool(target_cfg.get("rework_cleanup", False))
    rework_attempts = int(ctx.state.setdefault("rework_attempts", {}).get(target_stage, 0))
    review = current_payload.get("review") or {}
    if review.get("pass") is False and review_rework_enabled and rework_attempts < rework_limit:
        if ctx.stage_logger:
            ctx.stage_logger(target_stage, "rework", {
                "label": ctx.stage_labels.get(target_stage, target_stage),
                "stage_type": target_type,
                "attempt": rework_attempts + 1,
                "feedback": review.get("feedback", ""),
                "cleanup": rework_cleanup,
            })
        if rework_cleanup:
            ctx.cleanup_artifacts(ctx.task, current_payload.get("artifacts") or [])
            if current_payload.get("artifacts"):
                keep = {str(a.get("uri")) for a in (current_payload.get("artifacts") or [])}
                ctx.state["artifacts"] = [a for a in ctx.state.get("artifacts", []) if str(a.get("uri")) not in keep]
        feedback = str(review.get("feedback") or "")
        rework_guidance = ctx.build_rework_guidance(target_type, feedback, rework_attempts)
        conversation_id = str(current_payload.get("conversation_id") or ctx.collaboration.ensure_thread(
            target_stage,
            stage_type=target_type,
            thread_kind="stage_loop",
            title=f"{ctx.stage_labels.get(target_stage, target_stage)} 协作线程",
        ))
        reviewer_feedback = ctx.collaboration.post_message(
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
            recipient_id=ctx.default_actor_id(target_stage, target_type),
            payload={**review, "rework_guidance": rework_guidance},
        )
        ctx.post_stage_status(
            target_stage,
            target_type,
            f"{ctx.stage_labels.get(target_stage, target_stage)} 已根据评审意见进入返工。",
            conversation_id=conversation_id,
            status_kind="review_rework",
            status_level="warning",
            actor_id=f"{target_stage}-system",
            payload={"feedback": feedback, "attempt": rework_attempts + 1},
        )
        ctx.collaboration.upsert_blackboard(
            entry_key=f"stage:{target_stage}:active_rework",
            title=f"{ctx.stage_labels.get(target_stage, target_stage)} 当前返工要求",
            content=((feedback + ("\n\n" + rework_guidance if rework_guidance else "")).strip() or "评审未通过，需要继续修复。"),
            entry_type="rework_request",
            stage_name=target_stage,
            payload={**review, "rework_guidance": rework_guidance},
            source_message_id=reviewer_feedback.get("message_id"),
        )
        ctx.state.setdefault("rework_attempts", {})[target_stage] = rework_attempts + 1
        current_payload = ctx.execute_stage_once(target_stage, reason="review_rework")
        if current_payload is None:
            return None
        review = current_payload.get("review") or {}
    if review.get("pass") is False and review_blocking:
        err = f"stage_review_failed:{target_stage}"
        if ctx.stage_logger:
            ctx.stage_logger(target_stage, "error", {
                "label": ctx.stage_labels.get(target_stage, target_stage),
                "stage_type": target_type,
                "error": err,
                "feedback": review.get("feedback", ""),
            })
        ctx.state["error"] = err
        return None
    return current_payload


def apply_coding_smoke_loop(
    ctx: WorkflowPolicyContext,
    current_payload: Payload,
    coding_stage: str,
    *,
    reason_prefix: str = "coding",
) -> Optional[Payload]:
    coding_cfg = ctx.get_stage_cfg(coding_stage)
    smoke_fix_limit = int(coding_cfg.get("auto_smoke_fix_limit", 2) or 2)
    smoke_blocking = bool(coding_cfg.get("smoke_test_blocking", True))
    smoke_attempts = int(ctx.state.setdefault("smoke_fix_attempts", {}).get(coding_stage, 0))
    while ctx.smoke_failed(current_payload) and smoke_attempts < smoke_fix_limit:
        smoke_attempts += 1
        ctx.state.setdefault("smoke_fix_attempts", {})[coding_stage] = smoke_attempts
        smoke_feedback = ctx.collect_smoke_feedback(current_payload)
        if ctx.stage_logger:
            ctx.stage_logger(coding_stage, "rework", {
                "label": ctx.stage_labels.get(coding_stage, coding_stage),
                "stage_type": ctx.stage_types_by_name.get(coding_stage, "coding"),
                "attempt": smoke_attempts,
                "reason": f"{reason_prefix}_smoke_failed",
                "feedback": smoke_feedback,
            })
        conversation_id = str(current_payload.get("conversation_id") or ctx.collaboration.ensure_thread(
            coding_stage,
            stage_type="coding",
            thread_kind="stage_loop",
            title=f"{ctx.stage_labels.get(coding_stage, coding_stage)} 协作线程",
        ))
        smoke_guidance = ctx.build_rework_guidance("coding", smoke_feedback, smoke_attempts - 1)
        smoke_message = ctx.collaboration.post_message(
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
            recipient_id=ctx.default_actor_id(coding_stage, "coding"),
            payload={"reason_prefix": reason_prefix, "feedback": smoke_feedback, "rework_guidance": smoke_guidance},
        )
        ctx.post_stage_status(
            coding_stage,
            "coding",
            f"{ctx.stage_labels.get(coding_stage, coding_stage)} 冒烟未通过，正在进行自动修复。",
            conversation_id=conversation_id,
            status_kind="smoke_rework",
            status_level="warning",
            actor_id=f"{coding_stage}-system",
            payload={"feedback": smoke_feedback, "attempt": smoke_attempts},
        )
        ctx.collaboration.upsert_blackboard(
            entry_key=f"stage:{coding_stage}:smoke_feedback",
            title=f"{ctx.stage_labels.get(coding_stage, coding_stage)} 冒烟反馈",
            content=((smoke_feedback + ("\n\n" + smoke_guidance if smoke_guidance else "")).strip() or "编码阶段冒烟校验失败。"),
            entry_type="smoke_feedback",
            stage_name=coding_stage,
            payload={"reason_prefix": reason_prefix, "feedback": smoke_feedback, "rework_guidance": smoke_guidance},
            source_message_id=smoke_message.get("message_id"),
        )
        current_payload = ctx.execute_stage_once(coding_stage, reason="smoke_fix")
        if current_payload is None:
            return None
        current_payload = apply_review_rework(ctx, coding_stage, current_payload)
        if current_payload is None:
            return None
    if ctx.smoke_failed(current_payload) and smoke_blocking:
        err = "coding_smoke_failed"
        if ctx.stage_logger:
            ctx.stage_logger(coding_stage, "error", {
                "label": ctx.stage_labels.get(coding_stage, coding_stage),
                "stage_type": ctx.stage_types_by_name.get(coding_stage, "coding"),
                "error": err,
                "feedback": ctx.collect_smoke_feedback(current_payload),
            })
        ctx.state["error"] = err
        return None
    return current_payload


def apply_testing_fix_loop(
    ctx: WorkflowPolicyContext,
    stage_name: str,
    current_stage_type: str,
    payload: Payload,
) -> Optional[Payload]:
    testing_cfg = ctx.get_stage_cfg(stage_name)
    test_fix_limit = int(testing_cfg.get("auto_fix_limit", 3) or 3)
    test_fix_attempts = int(ctx.state.setdefault("test_fix_attempts", {}).get(stage_name, 0))
    while ctx.testing_failed(payload) and test_fix_attempts < test_fix_limit:
        test_fix_attempts += 1
        ctx.state.setdefault("test_fix_attempts", {})[stage_name] = test_fix_attempts
        test_feedback = ctx.collect_test_feedback(payload)
        if ctx.stage_logger:
            ctx.stage_logger(stage_name, "rework", {
                "label": ctx.stage_labels.get(stage_name, stage_name),
                "stage_type": current_stage_type,
                "attempt": test_fix_attempts,
                "reason": "testing_failed",
                "feedback": test_feedback,
            })

        coding_stage = ctx.resolve_related_stage(stage_name, "coding")
        if not coding_stage:
            ctx.state["error"] = "testing_failed_without_coding_stage"
            return None
        handoff_conversation = ctx.collaboration.ensure_thread(
            coding_stage,
            stage_type="coding",
            thread_kind="testing_handoff",
            peer_stage=stage_name,
            title=f"{ctx.stage_labels.get(stage_name, stage_name)} -> {ctx.stage_labels.get(coding_stage, coding_stage)} 缺陷回传",
            participants=[
                {"actor_id": "tester", "role": ctx.stage_role_name(stage_name, "testing", "测试工程师")},
                {"actor_id": ctx.default_actor_id(coding_stage, "coding"), "role": ctx.stage_role_name(coding_stage, "coding", "软件工程师")},
            ],
        )
        testing_feedback_message = ctx.collaboration.post_message(
            stage_name=coding_stage,
            stage_type="coding",
            actor_id="tester",
            actor_role=ctx.stage_role_name(stage_name, "testing", "测试工程师"),
            content=(
                "全面测试阶段发现缺陷，请先修复后再回到全面测试。\n"
                f"{test_feedback}"
            ).strip(),
            message_type="test_feedback",
            conversation_id=handoff_conversation,
            thread_kind="testing_handoff",
            recipient_id=ctx.default_actor_id(coding_stage, "coding"),
            payload={"source_stage": stage_name, "feedback": test_feedback},
        )
        ctx.collaboration.upsert_blackboard(
            entry_key=f"stage:{coding_stage}:test_feedback",
            title=f"{ctx.stage_labels.get(coding_stage, coding_stage)} 最新测试反馈",
            content=test_feedback or "全面测试阶段发现缺陷。",
            entry_type="test_feedback",
            stage_name=coding_stage,
            payload={"source_stage": stage_name, "feedback": test_feedback},
            source_message_id=testing_feedback_message.get("message_id"),
        )

        coding_payload = ctx.execute_stage_once(coding_stage, reason="fix_from_testing")
        if coding_payload is None:
            return None
        coding_payload = apply_review_rework(ctx, coding_stage, coding_payload)
        if coding_payload is None:
            return None
        coding_payload = apply_coding_smoke_loop(ctx, coding_payload, coding_stage, reason_prefix="fix_from_testing")
        if coding_payload is None:
            return None

        payload = ctx.execute_stage_once(stage_name, reason="after_code_fix")
        if payload is None:
            return None
        payload = apply_review_rework(ctx, stage_name, payload)
        if payload is None:
            return None

    if ctx.testing_failed(payload):
        err = "testing_failed_after_rework"
        if ctx.stage_logger:
            ctx.stage_logger(stage_name, "error", {
                "label": ctx.stage_labels.get(stage_name, stage_name),
                "stage_type": current_stage_type,
                "error": err,
                "feedback": ctx.collect_test_feedback(payload),
            })
        ctx.state["error"] = err
        return None
    return payload
