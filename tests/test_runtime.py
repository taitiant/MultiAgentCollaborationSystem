from __future__ import annotations

from orchestration.execution.runtime import TaskRuntime


class _RecordingPlugin:
    def __init__(self):
        self.events = []

    def on_event(self, event, state):
        self.events.append((event.event_type, len(state.history)))


class _FailingPlugin:
    def on_event(self, event, state):
        raise RuntimeError("plugin failure should be isolated")


def test_task_runtime_records_event_and_notifies_plugins():
    logged = []
    plugin = _RecordingPlugin()
    runtime = TaskRuntime(
        event_logger=lambda *args: logged.append(args),
        plugins=(plugin,),
    )

    event = runtime.emit_event("agent", "task-1", "StageDone", {"stage": "coding"})

    assert runtime.state.history[-1] == event
    assert logged[0][1] == "task-1"
    assert plugin.events == [("StageDone", 1)]


def test_task_runtime_isolates_plugin_failures():
    plugin = _RecordingPlugin()
    runtime = TaskRuntime(plugins=(_FailingPlugin(), plugin))

    runtime.emit_event("agent", "task-2", "GraphRun", {})

    assert plugin.events == [("GraphRun", 1)]


def test_task_runtime_drop_task_clears_state_and_marks_aborted():
    runtime = TaskRuntime()
    runtime.state.tasks["task-3"] = object()
    runtime.state.task_status["task-3"] = "running"
    runtime.emit_event("agent", "task-3", "TaskCreated", {})

    runtime.drop_task("task-3")

    assert "task-3" not in runtime.state.tasks
    assert "task-3" not in runtime.state.task_status
    assert runtime.is_task_aborted("task-3") is True
    assert runtime.state.history == []
