from __future__ import annotations

from core import Task
from orchestration.application.tasks import TaskApplicationService


def test_task_application_service_delegates_step_and_rerun():
    task = Task(task_id="task-1", domain="software", required_capabilities=[], context={})
    ensured = []
    service = TaskApplicationService(
        create_task_fn=lambda body: {"created": body["task_id"]},
        run_step_fn=lambda current_task: {"status": "step", "task_id": current_task.task_id},
        run_single_stage_fn=lambda current_task, stage: {"status": "rerun", "task_id": current_task.task_id, "stage": stage},
        abort_task_fn=lambda task_id: {"status": "aborting", "task_id": task_id},
        submit_human_decision_fn=lambda task_id, body: {"status": "ok", "task_id": task_id, "decision": body["decision"]},
        ensure_task_fn=lambda task_id: ensured.append(task_id) or task,
    )

    step_result = service.step_task("task-1")
    rerun_result = service.rerun_stage("task-1", "coding")

    assert step_result == {"status": "step", "task_id": "task-1"}
    assert rerun_result == {"status": "rerun", "task_id": "task-1", "stage": "coding"}
    assert ensured == ["task-1", "task-1"]


def test_task_application_service_checks_task_before_abort_and_decision():
    task = Task(task_id="task-2", domain="software", required_capabilities=[], context={})
    ensured = []
    service = TaskApplicationService(
        create_task_fn=lambda body: body,
        run_step_fn=lambda current_task: {"task_id": current_task.task_id},
        run_single_stage_fn=lambda current_task, stage: {"task_id": current_task.task_id, "stage": stage},
        abort_task_fn=lambda task_id: {"status": "aborting", "task_id": task_id},
        submit_human_decision_fn=lambda task_id, body: {"status": "ok", "task_id": task_id, "body": body},
        ensure_task_fn=lambda task_id: ensured.append(task_id) or task,
    )

    abort_result = service.abort_task("task-2")
    decision_result = service.submit_human_decision("task-2", {"decision": "A"})

    assert abort_result == {"status": "aborting", "task_id": "task-2"}
    assert decision_result == {"status": "ok", "task_id": "task-2", "body": {"decision": "A"}}
    assert ensured == ["task-2", "task-2"]
