"""阶段 Agent 注册表，负责把阶段类型映射到具体 Agent 工厂。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol

from domains.software_dev.agents.asset_agent import AssetAgent
from domains.software_dev.agents.document_agents import ArchAgent, DocAgent, ReqAgent
from domains.software_dev.agents.patch_agent import PatchAgent
from domains.software_dev.agents.test_agent import TestAgent


@dataclass(frozen=True)
class StageAgentRequest:
    stage_name: str
    stage_type: str
    prompt_template: str | None
    model_adapter: Any = None
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None


class StageAgentFactory(Protocol):
    def __call__(self, request: StageAgentRequest) -> Any: ...


class StageAgentRegistry:
    def __init__(self) -> None:
        self._factories: Dict[str, StageAgentFactory] = {}

    def register(self, stage_type: str, factory: StageAgentFactory) -> None:
        self._factories[str(stage_type or "").strip()] = factory

    def create(self, request: StageAgentRequest) -> Any:
        factory = self._factories.get(str(request.stage_type or "").strip())
        if not factory:
            raise ValueError(f"unknown stage type: {request.stage_type}")
        return factory(request)


def build_default_stage_agent_registry() -> StageAgentRegistry:
    registry = StageAgentRegistry()

    registry.register(
        "requirements",
        lambda request: ReqAgent(
            request.model_adapter,
            stage_name=request.stage_name,
            stage_type=request.stage_type,
            prompt_template=request.prompt_template,
            progress_callback=request.progress_callback,
        ),
    )
    registry.register(
        "architecture",
        lambda request: ArchAgent(
            request.model_adapter,
            stage_name=request.stage_name,
            stage_type=request.stage_type,
            prompt_template=request.prompt_template,
            progress_callback=request.progress_callback,
        ),
    )
    registry.register(
        "assets",
        lambda request: AssetAgent(
            request.model_adapter,
            stage_name=request.stage_name,
            stage_type=request.stage_type,
            prompt_template=request.prompt_template,
            progress_callback=request.progress_callback,
        ),
    )
    registry.register(
        "coding",
        lambda request: PatchAgent(
            model_adapter=request.model_adapter,
            stage_name=request.stage_name,
            stage_type=request.stage_type,
            progress_callback=request.progress_callback,
        ),
    )
    registry.register(
        "testing",
        lambda request: TestAgent(
            stage_name=request.stage_name,
            stage_type=request.stage_type,
            progress_callback=request.progress_callback,
        ),
    )
    registry.register(
        "docs",
        lambda request: DocAgent(
            request.model_adapter,
            stage_name=request.stage_name,
            stage_type=request.stage_type,
            prompt_template=request.prompt_template,
            progress_callback=request.progress_callback,
        ),
    )
    return registry
