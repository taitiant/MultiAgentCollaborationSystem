from __future__ import annotations

from orchestration.execution.stage_execution import StageExecutionContext, StageExecutionService


class _FakeAgent:
    id = "req-analyst"
    role_name = "ReqAgent"

    def act(self, _task, _state):
        return {"type": "md", "filename": "analysis/requirements.md", "content": "hello"}


class _FakeCollaboration:
    def __init__(self):
        self.messages = []

    def ensure_thread(self, *args, **kwargs):
        return "conv-1"

    def post_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"message_id": f"msg-{len(self.messages)}"}

    def upsert_blackboard(self, **kwargs):
        self.messages.append({"blackboard": kwargs})


class _FakeCapabilityRuntime:
    def apply_stage_capabilities(self, **kwargs):
        return dict(kwargs["payload"])


def test_stage_execution_service_executes_stage_and_adds_review():
    task = type("Task", (), {"task_id": "task-1", "context": {}})()
    state = {}
    artifacts = []
    collaboration = _FakeCollaboration()
    statuses = []
    submissions = []

    def write_text(_base_dir, _task_id, filename, content):
        path = f"/tmp/{filename}"
        artifacts.append({"path": path, "content": content})
        return path

    ctx = StageExecutionContext(
        task=task,
        state=state,
        base_dir="/tmp",
        stage_labels={"requirements": "需求分析"},
        stage_types_by_name={"requirements": "requirements"},
        stage_caps={"requirements": []},
        collaboration=collaboration,
        capability_runtime=_FakeCapabilityRuntime(),
        storage=None,
        stage_logger=None,
        should_abort=lambda _task: False,
        create_agent=lambda _stage: _FakeAgent(),
        get_stage_cfg=lambda _stage: {},
        default_actor_id=lambda stage, _stage_type: stage,
        stage_role_name=lambda _stage, _stage_type, fallback: fallback,
        apply_runtime_collaboration_context=lambda _stage: None,
        clear_runtime_collaboration_context=lambda: None,
        post_stage_status=lambda *args, **kwargs: statuses.append((args, kwargs)) or "conv-1",
        record_stage_submission=lambda *args, **kwargs: submissions.append((args, kwargs)) or "conv-1",
        write_text=write_text,
        write_runtime_artifact=lambda artifact: artifact,
        review_stage_output=lambda _task, _stage, payload, _stage_type, _progress: {"pass": True, "artifact_count": len(payload.get("artifacts") or [])},
        resolve_related_stage=lambda _stage, _stage_type: None,
        extract_human_decision_request=lambda *_args, **_kwargs: None,
    )

    service = StageExecutionService(ctx)
    result = service.execute_once("requirements")

    assert result["stage"] == "requirements"
    assert result["review"]["pass"] is True
    assert result["artifacts"][0]["uri"] == "/tmp/analysis/requirements.md"
    assert submissions
