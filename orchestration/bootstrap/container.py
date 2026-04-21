"""应用装配容器，负责组装配置、基础设施与执行服务。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict

import db
from adapters.model_registry import ModelRegistry
from core import SystemState
from orchestration.capabilities.registry import merge_capability_settings, sync_capability_settings_with_skills
from orchestration.execution.runtime import TaskRuntime
from orchestration.execution.workflow_runner import WorkflowRunner
from orchestration.graph_builder import GraphBuilder
from orchestration.mcp.registry import merge_mcp_settings
from orchestration.planning.stage_catalog import STAGE_TYPE_BLUEPRINTS
from orchestration.skills.registry import merge_skill_settings
from plugins.logging_plugin import LoggingPlugin
from plugins.metrics_plugin import MetricsPlugin
from storage.file_store import FileStore


@dataclass(frozen=True)
class AppPaths:
    base_dir: str
    capability_config_path: str
    mcp_config_path: str
    skill_config_path: str
    workspace_root: str
    workflow_template_path: str


@dataclass
class AppConfigContainer:
    paths: AppPaths
    workflow_template: Dict[str, Any]
    workflow_stages: list[str]
    workflow_stage_map: Dict[str, Dict[str, Any]]
    execution_profile_keys: list[str]
    capability_config: Dict[str, Any]
    mcp_config: Dict[str, Any]
    skill_config: Dict[str, Any]


@dataclass
class InfrastructureContainer:
    storage: FileStore
    model_registry: ModelRegistry
    logging_plugin: LoggingPlugin
    metrics_plugin: MetricsPlugin


@dataclass
class ExecutionContainer:
    graph_builder: GraphBuilder
    runtime: TaskRuntime
    workflow_runner: WorkflowRunner


@dataclass
class AppContainer:
    config: AppConfigContainer
    infrastructure: InfrastructureContainer
    execution: ExecutionContainer


def _load_json_config(path: str, loader):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return loader(json.load(handle))
    return loader()


def build_app_container(base_dir: str) -> AppContainer:
    paths = AppPaths(
        base_dir=base_dir,
        capability_config_path=os.path.join(base_dir, "config", "capabilities.json"),
        mcp_config_path=os.path.join(base_dir, "config", "mcp_servers.json"),
        skill_config_path=os.path.join(base_dir, "config", "skills.json"),
        workspace_root=os.path.join(base_dir, "workspace"),
        workflow_template_path=os.path.join(base_dir, "config", "workflow_templates", "software_dev.json"),
    )

    with open(paths.workflow_template_path, "r", encoding="utf-8") as handle:
        workflow_template = json.load(handle)

    storage = FileStore(base_path=paths.workspace_root)
    model_registry = ModelRegistry()
    logging_plugin = LoggingPlugin()
    metrics_plugin = MetricsPlugin()
    db.init_db()

    capability_config = _load_json_config(paths.capability_config_path, merge_capability_settings)
    mcp_config = _load_json_config(paths.mcp_config_path, merge_mcp_settings)
    skill_config = _load_json_config(paths.skill_config_path, merge_skill_settings)
    capability_config = sync_capability_settings_with_skills(capability_config, skill_config)

    graph_builder = GraphBuilder(
        paths.workspace_root,
        model_registry,
        capability_settings_provider=lambda: capability_config,
        skill_settings_provider=lambda: skill_config,
        mcp_settings_provider=lambda: mcp_config,
    )
    runtime = TaskRuntime(
        status_updater=lambda task_id, status: db.update_task_status(task_id, status),
        event_logger=lambda event_id, task_id, actor_id, event_type, payload, timestamp: db.log_event(
            event_id, task_id, actor_id, event_type, payload, timestamp
        ),
        plugins=(logging_plugin, metrics_plugin),
        state=SystemState(tasks={}, task_status={}, history=[]),
    )
    workflow_runner = WorkflowRunner(
        runtime=runtime,
        graph_builder=graph_builder,
        should_abort=runtime.is_task_aborted,
        update_task_context=db.update_task_context,
    )

    config = AppConfigContainer(
        paths=paths,
        workflow_template=workflow_template,
        workflow_stages=[st.get("name") for st in workflow_template.get("stages", [])],
        workflow_stage_map={st.get("name"): st for st in workflow_template.get("stages", [])},
        execution_profile_keys=list(STAGE_TYPE_BLUEPRINTS.keys()),
        capability_config=capability_config,
        mcp_config=mcp_config,
        skill_config=skill_config,
    )
    infrastructure = InfrastructureContainer(
        storage=storage,
        model_registry=model_registry,
        logging_plugin=logging_plugin,
        metrics_plugin=metrics_plugin,
    )
    execution = ExecutionContainer(
        graph_builder=graph_builder,
        runtime=runtime,
        workflow_runner=workflow_runner,
    )
    return AppContainer(
        config=config,
        infrastructure=infrastructure,
        execution=execution,
    )
