from __future__ import annotations

import server.app as app_module
from core import Task


class _DummyGraph:
    def __init__(self, result):
        self._result = result

    def invoke(self, _state):
        return dict(self._result)


def _reset_runtime_state():
    app_module.state.tasks = {}
    app_module.state.task_status = {}
    app_module.state.history = []


def test_run_step_marks_finished_planned_task_completed(monkeypatch, tmp_path):
    _reset_runtime_state()
    task = Task(
        task_id="task-finished-plan",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "spec": "demo",
            "leader_plan": {
                "stages": [
                    {"name": "requirements", "stage_type": "requirements"},
                    {"name": "implementation", "stage_type": "coding"},
                ]
            },
            "event_configs": {},
        },
        workspace_path=str(tmp_path / "workspace"),
    )
    app_module.state.tasks[task.task_id] = task
    app_module.state.task_status[task.task_id] = "created"

    status_updates = []
    monkeypatch.setattr(app_module, "ensure_task_defaults", lambda _task: False)
    monkeypatch.setattr(app_module.db, "update_task_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.db, "update_task_status", lambda task_id, status: status_updates.append((task_id, status)))
    monkeypatch.setattr(
        app_module.db,
        "get_events",
        lambda task_id, limit=1000: [
            {"event_id": "evt-1", "task_id": task_id, "event_type": "StageDone", "payload": {"stage": "requirements"}, "timestamp": 1.0},
            {"event_id": "evt-2", "task_id": task_id, "event_type": "StageDone", "payload": {"stage": "implementation"}, "timestamp": 2.0},
        ],
    )
    monkeypatch.setattr(app_module, "write_leader_plan_snapshot", lambda *_args, **_kwargs: str(tmp_path / "leader_plan.json"))
    monkeypatch.setattr(
        app_module.graph_builder,
        "plan_workflow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not replan a finished workflow")),
    )
    monkeypatch.setattr(
        app_module.graph_builder,
        "build",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not rebuild a finished workflow")),
    )

    result = app_module.run_step(task)

    assert result == {"status": "completed", "artifacts": []}
    assert app_module.state.task_status[task.task_id] == "completed"
    assert status_updates == [(task.task_id, "completed")]


def test_run_step_sets_running_before_full_execution(monkeypatch, tmp_path):
    _reset_runtime_state()
    task = Task(
        task_id="task-run-sequence",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "写一个简单工具", "event_configs": {}},
        workspace_path=str(tmp_path / "workspace"),
    )
    app_module.state.tasks[task.task_id] = task
    app_module.state.task_status[task.task_id] = "created"

    status_updates = []
    monkeypatch.setattr(app_module, "ensure_task_defaults", lambda _task: False)
    monkeypatch.setattr(app_module.db, "update_task_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.db, "update_task_status", lambda task_id, status: status_updates.append((task_id, status)))
    monkeypatch.setattr(app_module.db, "log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.logging_plugin, "on_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.metrics_plugin, "on_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        app_module.graph_builder,
        "plan_workflow",
        lambda *_args, **_kwargs: {"stages": [{"name": "requirements", "stage_type": "requirements"}]},
    )
    monkeypatch.setattr(
        app_module.graph_builder,
        "build",
        lambda *_args, **_kwargs: _DummyGraph({"artifacts": [{"type": "md", "uri": str(tmp_path / "requirements.md")}]}),
    )

    result = app_module.run_step(task)

    assert result["status"] == "completed"
    assert result["artifacts"] == [{"type": "md", "uri": str(tmp_path / "requirements.md")}]
    assert [status for _, status in status_updates] == ["running", "completed"]


def test_run_step_blocks_when_pending_human_decision_exists(monkeypatch, tmp_path):
    _reset_runtime_state()
    task = Task(
        task_id="task-pending-decision",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "spec": "demo",
            "pending_human_decision": {
                "kind": "human_decision",
                "stage": "requirements_gate",
                "stage_type": "requirements",
                "label": "范围澄清",
                "question": "选 A 还是 B？",
            },
        },
        workspace_path=str(tmp_path / "workspace"),
    )
    app_module.state.tasks[task.task_id] = task
    app_module.state.task_status[task.task_id] = "created"

    status_updates = []
    monkeypatch.setattr(app_module.db, "update_task_status", lambda task_id, status: status_updates.append((task_id, status)))

    result = app_module.run_step(task)

    assert result["status"] == "await_user"
    assert result["await"]["question"] == "选 A 还是 B？"
    assert status_updates == [(task.task_id, "waiting_user")]


def test_submit_human_decision_requires_pending_request(monkeypatch, tmp_path):
    _reset_runtime_state()
    task = Task(
        task_id="task-no-pending-decision",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "demo"},
        workspace_path=str(tmp_path / "workspace"),
    )
    app_module.state.tasks[task.task_id] = task

    try:
        app_module.submit_human_decision(task.task_id, {"decision": "我选择方案 A"})
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
    else:
        raise AssertionError("expected HTTPException when no pending decision exists")


def test_submit_human_decision_records_message_and_clears_pending(monkeypatch, tmp_path):
    _reset_runtime_state()
    task = Task(
        task_id="task-submit-decision",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "spec": "demo",
            "leader_plan": {
                "stages": [
                    {"name": "requirements_gate", "stage_type": "requirements", "label": "范围澄清"},
                ]
            },
            "pending_human_decision": {
                "kind": "human_decision",
                "stage": "requirements_gate",
                "stage_type": "requirements",
                "label": "范围澄清",
                "question": "优先做桌面版还是浏览器版？",
                "options": ["桌面版", "浏览器版"],
                "why_blocked": "路线不同会影响后续文件结构。",
            },
        },
        workspace_path=str(tmp_path / "workspace"),
    )
    app_module.state.tasks[task.task_id] = task
    app_module.state.task_status[task.task_id] = "waiting_user"

    monkeypatch.setattr(app_module.db, "update_task_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.db, "log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.db, "update_task_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.logging_plugin, "on_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.metrics_plugin, "on_event", lambda *_args, **_kwargs: None)

    result = app_module.submit_human_decision(
        task.task_id,
        {"selected_option": "浏览器版", "decision": "优先做浏览器版，后续更容易直接预览。"},
    )

    assert result["status"] == "ok"
    assert task.context.get("pending_human_decision") is None
    assert task.context["human_decision_history"][-1]["selected_option"] == "浏览器版"
    messages = app_module.db.list_conversation_messages(task.task_id, stage_name="requirements_gate", limit=20)
    assert any(message["message_type"] == "user_decision" for message in messages)
