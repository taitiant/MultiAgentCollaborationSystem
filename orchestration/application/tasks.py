"""任务生命周期应用服务，对外提供 HTTP 层使用的任务动作入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict

from core import Task


@dataclass
class TaskApplicationService:
    create_task_fn: Callable[[Dict[str, Any]], Dict[str, Any]]
    run_step_fn: Callable[[Task], Dict[str, Any]]
    run_single_stage_fn: Callable[[Task, str], Dict[str, Any]]
    abort_task_fn: Callable[[str], Dict[str, Any]]
    submit_human_decision_fn: Callable[[str, Dict[str, Any]], Dict[str, Any]]
    ensure_task_fn: Callable[[str], Task]

    def create_task(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.create_task_fn(body)

    def step_task(self, task_id: str) -> Dict[str, Any]:
        task = self.ensure_task_fn(task_id)
        return self.run_step_fn(task)

    def rerun_stage(self, task_id: str, stage_name: str) -> Dict[str, Any]:
        task = self.ensure_task_fn(task_id)
        return self.run_single_stage_fn(task, stage_name)

    def abort_task(self, task_id: str) -> Dict[str, Any]:
        self.ensure_task_fn(task_id)
        return self.abort_task_fn(task_id)

    def submit_human_decision(self, task_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_task_fn(task_id)
        return self.submit_human_decision_fn(task_id, body)
