from __future__ import annotations

from orchestration.execution.stage_agent_registry import StageAgentRequest, build_default_stage_agent_registry


def test_default_stage_agent_registry_creates_document_agent():
    registry = build_default_stage_agent_registry()

    agent = registry.create(
        StageAgentRequest(
            stage_name="requirements",
            stage_type="requirements",
            prompt_template="请输出需求",
            model_adapter=object(),
        )
    )

    assert agent.__class__.__name__ == "ReqAgent"
    assert agent.stage_name == "requirements"


def test_default_stage_agent_registry_creates_testing_agent_without_model():
    registry = build_default_stage_agent_registry()

    agent = registry.create(
        StageAgentRequest(
            stage_name="qa",
            stage_type="testing",
            prompt_template=None,
        )
    )

    assert agent.__class__.__name__ == "TestAgent"
    assert agent.stage_name == "qa"
