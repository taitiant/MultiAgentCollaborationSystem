"""工作流图执行协调器，连接运行时状态与已编译工作流。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict

from core import Task, new_event
from orchestration.execution.runtime import TaskRuntime


StageLogger = Callable[[str, str, Dict[str, Any]], None]
AbortChecker = Callable[[str], bool]


@dataclass
class WorkflowRunner:
    runtime: TaskRuntime
    graph_builder: Any
    should_abort: AbortChecker
    update_task_context: Callable[[str, Dict[str, Any]], None]

    def _build_stage_logger(self, task: Task) -> StageLogger:
        def stage_logger(stage: str, status: str, payload: Dict[str, Any]) -> None:
            if self.should_abort(task.task_id):
                return
            self.runtime.record_event(
                new_event("graph", task.task_id, "Stage" + status.capitalize(), {"stage": stage, **(payload or {})})
            )

        return stage_logger

    def invoke(self, task: Task, template: Dict[str, Any]) -> Dict[str, Any]:
        graph = self.graph_builder.build(
            task,
            template,
            stage_logger=self._build_stage_logger(task),
            should_abort=lambda current_task: self.should_abort(current_task.task_id),
        )
        result = graph.invoke({"task": task, "artifacts": []})
        if task.task_id not in self.runtime.state.tasks:
            return {"status": "deleted", "task_id": task.task_id}
        return result

    def persist_context(self, task: Task) -> None:
        self.update_task_context(task.task_id, task.context)

    def record_graph_error(self, task: Task, error: str):
        return self.runtime.record_event(new_event("graph", task.task_id, "GraphError", {"error": error}))

    def record_graph_abort(self, task: Task, abort_payload: Dict[str, Any]):
        return self.runtime.record_event(new_event("graph", task.task_id, "GraphAbort", {"abort": abort_payload}))

    def record_graph_run(self, task: Task, result: Dict[str, Any]):
        return self.runtime.emit_event(
            "graph",
            task.task_id,
            "GraphRun",
            {"await": result.get("await"), "artifacts": result.get("artifacts", [])},
        )

    def record_stage_event(self, actor_id: str, task: Task, event_type: str, payload: Dict[str, Any]):
        return self.runtime.record_event(new_event(actor_id, task.task_id, event_type, payload))
