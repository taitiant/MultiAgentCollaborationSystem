from __future__ import annotations

from orchestration.execution.workflow_policy import (
    WorkflowPolicyContext,
    apply_coding_smoke_loop,
    apply_review_rework,
)


class _FakeCollaboration:
    def __init__(self):
        self.messages = []
        self.blackboards = []

    def ensure_thread(self, *args, **kwargs):
        return "conv-1"

    def post_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"message_id": f"msg-{len(self.messages)}"}

    def upsert_blackboard(self, **kwargs):
        self.blackboards.append(kwargs)


def _build_context(**overrides):
    state = {}
    collaboration = _FakeCollaboration()
    executed = []

    def execute_stage_once(stage, reason=None):
        executed.append((stage, reason))
        return {
            "stage": stage,
            "review": {"pass": True},
            "conversation_id": "conv-1",
            "artifacts": [],
        }

    ctx = WorkflowPolicyContext(
        state=state,
        collaboration=collaboration,
        execute_stage_once=execute_stage_once,
        post_stage_status=lambda *args, **kwargs: "conv-1",
        get_stage_cfg=lambda _stage: {},
        resolve_related_stage=lambda _stage, stage_type: "coding" if stage_type == "coding" else None,
        default_actor_id=lambda stage, _stage_type: f"{stage}-agent",
        stage_role_name=lambda _stage, stage_type, fallback: fallback or stage_type,
        stage_logger=None,
        cleanup_artifacts=lambda *_args, **_kwargs: None,
        smoke_failed=lambda payload: payload.get("smoke_failed") is True,
        testing_failed=lambda payload: payload.get("test_failed") is True,
        collect_smoke_feedback=lambda _payload: "smoke failed",
        collect_test_feedback=lambda _payload: "test failed",
        is_review_blocking=lambda _stage_type, _cfg: True,
        build_rework_guidance=lambda _stage_type, feedback, attempt: f"{feedback}:{attempt}",
        task=object(),
        stage_labels={"coding": "编码阶段"},
        stage_types_by_name={"coding": "coding"},
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx, executed, collaboration


def test_apply_review_rework_requests_retry_and_updates_state():
    ctx, executed, collaboration = _build_context()
    payload = {
        "review": {"pass": False, "feedback": "please fix"},
        "conversation_id": "conv-1",
        "artifacts": [],
    }

    result = apply_review_rework(ctx, "coding", payload)

    assert result["review"]["pass"] is True
    assert executed == [("coding", "review_rework")]
    assert ctx.state["rework_attempts"]["coding"] == 1
    assert collaboration.messages[0]["message_type"] == "rework_request"


def test_apply_coding_smoke_loop_requests_smoke_fix():
    ctx, executed, collaboration = _build_context()
    ctx.get_stage_cfg = lambda _stage: {"auto_smoke_fix_limit": 1, "smoke_test_blocking": True}
    payload = {
        "stage": "coding",
        "conversation_id": "conv-1",
        "review": {"pass": True},
        "smoke_failed": True,
    }

    result = apply_coding_smoke_loop(ctx, payload, "coding")

    assert result["review"]["pass"] is True
    assert executed == [("coding", "smoke_fix")]
    assert ctx.state["smoke_fix_attempts"]["coding"] == 1
    assert collaboration.messages[0]["message_type"] == "smoke_feedback"
