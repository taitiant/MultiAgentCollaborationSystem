"""运行时状态容器，负责任务状态、事件发布与中止控制。"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Iterable, Optional

from core import Event, SystemState, new_event


StatusUpdater = Callable[[str, str], object]
EventLogger = Callable[[str, str, str, str, dict, float], object]


@dataclass
class TaskRuntime:
    status_updater: Optional[StatusUpdater] = None
    event_logger: Optional[EventLogger] = None
    plugins: Iterable[object] = field(default_factory=tuple)
    state: SystemState = field(default_factory=SystemState)

    def __post_init__(self) -> None:
        self._control_lock = Lock()
        self._aborted_tasks: set[str] = set()

    def is_task_aborted(self, task_id: str) -> bool:
        with self._control_lock:
            return task_id in self._aborted_tasks

    def mark_task_aborted(self, task_id: str) -> None:
        with self._control_lock:
            self._aborted_tasks.add(task_id)

    def clear_task_abort(self, task_id: str) -> None:
        with self._control_lock:
            self._aborted_tasks.discard(task_id)

    def set_task_status(self, task_id: str, status: str) -> str:
        self.state.task_status[task_id] = status
        if self.status_updater:
            self.status_updater(task_id, status)
        return status

    def record_event(self, event: Event) -> Event:
        self.state.history.append(event)
        if self.event_logger:
            self.event_logger(
                event.event_id,
                event.task_id,
                event.actor_id,
                event.event_type,
                event.payload,
                event.timestamp,
            )
        for plugin in self.plugins:
            on_event = getattr(plugin, "on_event", None)
            if callable(on_event):
                try:
                    on_event(event, self.state)
                except Exception:
                    continue
        return event

    def emit_event(self, actor_id: str, task_id: str, event_type: str, payload: dict) -> Event:
        return self.record_event(new_event(actor_id, task_id, event_type, payload))

    def drop_task(self, task_id: str) -> None:
        self.mark_task_aborted(task_id)
        self.state.tasks.pop(task_id, None)
        self.state.task_status.pop(task_id, None)
        self.state.history = [event for event in self.state.history if event.task_id != task_id]
