"""Scheduler implementations (Manual, CapabilityMatching)."""
from __future__ import annotations
from typing import Dict, List, Optional
from .base import BaseScheduler, Task, SystemState


class ManualScheduler(BaseScheduler):
    def __init__(self, manual_map: Dict[str, str]):
        # manual_map: task_id -> agent_id
        self.manual_map = manual_map

    def select_agent(self, task: Task, state: SystemState) -> Optional[str]:
        return self.manual_map.get(task.task_id)


class CapabilityMatchingScheduler(BaseScheduler):
    def __init__(self, agent_cap_index: Dict[str, List[str]], fallback: Optional[BaseScheduler] = None):
        # agent_cap_index: agent_id -> capabilities
        self.agent_cap_index = agent_cap_index
        self.fallback = fallback

    def select_agent(self, task: Task, state: SystemState) -> Optional[str]:
        required = set(task.required_capabilities)
        for agent_id, caps in self.agent_cap_index.items():
            if required.issubset(set(caps)):
                return agent_id
        return self.fallback.select_agent(task, state) if self.fallback else None
