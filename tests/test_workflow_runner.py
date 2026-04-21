from __future__ import annotations

from core import SystemState, Task
from orchestration.execution.runtime import TaskRuntime
from orchestration.execution.workflow_runner import WorkflowRunner


class _DummyGraph:
    def __init__(self, result):
        self._result = result

    def invoke(self, _state):
        return dict(self._result)


class _DummyBuilder:
    def __init__(self, result):
        self.result = result
        self.stage_logger = None

    def build(self, _task, _template, stage_logger=None, should_abort=None):
        self.stage_logger = stage_logger
        return _DummyGraph(self.result)


def test_workflow_runner_invokes_graph_and_persists_stage_events():
    runtime = TaskRuntime(state=SystemState(tasks={}, task_status={}, history=[]))
    task = Task(task_id="task-runner", domain="software", required_capabilities=[], context={})
    runtime.state.tasks[task.task_id] = task
    builder = _DummyBuilder({"artifacts": [{"type": "md"}]})
    persisted = []
    runner = WorkflowRunner(
        runtime=runtime,
        graph_builder=builder,
        should_abort=lambda _task_id: False,
        update_task_context=lambda task_id, context: persisted.append((task_id, dict(context))),
    )

    result = runner.invoke(task, {"stages": []})
    builder.stage_logger("requirements", "start", {"label": "需求"})
    runner.persist_context(task)

    assert result["artifacts"] == [{"type": "md"}]
    assert runtime.state.history[-1].event_type == "StageStart"
    assert persisted == [("task-runner", {})]


def test_workflow_runner_returns_deleted_when_task_is_removed():
    runtime = TaskRuntime(state=SystemState(tasks={}, task_status={}, history=[]))
    task = Task(task_id="task-deleted", domain="software", required_capabilities=[], context={})
    builder = _DummyBuilder({"artifacts": []})
    runner = WorkflowRunner(
        runtime=runtime,
        graph_builder=builder,
        should_abort=lambda _task_id: False,
        update_task_context=lambda *_args, **_kwargs: None,
    )

    result = runner.invoke(task, {"stages": []})

    assert result == {"status": "deleted", "task_id": "task-deleted"}
