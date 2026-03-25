"""Core abstractions for Multi-Agent Collaboration Runtime (Python skeleton)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Protocol
import uuid
import time

# === Data models ===
@dataclass
class Task:
    task_id: str
    domain: str
    required_capabilities: List[str]
    context: Dict[str, Any]
    priority: int = 0
    workspace_path: Optional[str] = None


@dataclass
class AgentMessage:
    message_id: str
    task_id: str
    actor_id: str
    domain: str
    intent: str
    capabilities_used: List[str]
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Event:
    event_id: str
    timestamp: float
    actor_id: str
    task_id: str
    event_type: str
    payload: Dict[str, Any]


@dataclass
class SystemState:
    tasks: Dict[str, Task] = field(default_factory=dict)
    task_status: Dict[str, str] = field(default_factory=dict)
    history: List[Event] = field(default_factory=list)
    # extend with queues, snapshots etc.


# === Protocols / Interfaces ===
class BaseModelAdapter(Protocol):
    model_name: str
    config: Dict[str, Any]

    def generate(self, prompt: str, context: Dict[str, Any]) -> str: ...


class BaseAgent(Protocol):
    id: str
    role_name: str
    domain: str
    capabilities: List[str]
    model_adapter: BaseModelAdapter

    def act(self, task: Task, state: SystemState) -> AgentMessage: ...


class BaseScheduler(Protocol):
    def select_agent(self, task: Task, state: SystemState) -> Optional[str]: ...


class BasePlugin(Protocol):
    def on_event(self, event: Event, state: SystemState) -> None: ...


class StorageAdapter(Protocol):
    def put(self, task_id: str, rel_path: str, data: bytes) -> str: ...
    def get(self, uri: str) -> bytes: ...
    def list(self, task_id: str, prefix: str = "") -> List[str]: ...
    def delete(self, uri: str) -> None: ...


# === Helpers ===
def new_event(actor_id: str, task_id: str, event_type: str, payload: Dict[str, Any]) -> Event:
    return Event(
        event_id=str(uuid.uuid4()),
        timestamp=time.time(),
        actor_id=actor_id,
        task_id=task_id,
        event_type=event_type,
        payload=payload,
    )


def new_message(actor_id: str, task: Task, intent: str, capabilities_used: List[str], artifacts=None, metadata=None) -> AgentMessage:
    return AgentMessage(
        message_id=str(uuid.uuid4()),
        task_id=task.task_id,
        actor_id=actor_id,
        domain=task.domain,
        intent=intent,
        capabilities_used=capabilities_used,
        artifacts=artifacts or [],
        metadata=metadata or {},
    )
