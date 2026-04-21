from __future__ import annotations

from orchestration.bootstrap.container import build_app_container


def test_build_app_container_loads_core_components():
    container = build_app_container("/Users/zjl/Desktop/Project/MySys/MultiAgentCollaborationSystem")

    assert container.config.paths.workspace_root.endswith("/workspace")
    assert isinstance(container.config.workflow_stages, list)
    assert "requirements" in container.config.execution_profile_keys
    assert container.infrastructure.storage is not None
    assert container.execution.graph_builder is not None
    assert container.execution.runtime is not None
    assert container.execution.workflow_runner is not None
