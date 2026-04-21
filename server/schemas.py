"""公共 HTTP API 使用的 Pydantic 请求与响应模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: Optional[str] = None
    required_capabilities: List[str] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)
    priority: int = 50
    domain: str = "software"
    workspace_path: Optional[str] = None


class SetTaskModelRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model_provider: Optional[str] = None


class SubmitHumanDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    decision: Optional[str] = None
    message: Optional[str] = None
    selected_option: Optional[str] = None


class UpdateTaskEventConfigRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model_provider: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    timeout: Optional[int] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key: Optional[str] = None
    notes: Optional[str] = None
    prompt_template: Optional[str] = None
    test_command: Optional[str] = None
    full_test_command: Optional[str] = None
    smoke_test_command: Optional[str] = None
    auto_fix_limit: Optional[int] = None
    auto_smoke_fix_limit: Optional[int] = None
    auto_rework_limit: Optional[int] = None
    review_blocking: Optional[bool] = None
    smoke_test_blocking: Optional[bool] = None
    rework_cleanup: Optional[bool] = None
    targeted_rework_enabled: Optional[bool] = None
    acceptance_criteria: Optional[str] = None
    planned_role: Optional[str] = None


class TaskWorkspaceResponse(BaseModel):
    task_id: str
    workspace_path: str


class TaskStatusResponse(BaseModel):
    status: str
    task_id: Optional[str] = None
    stage: Optional[str] = None
    workspace_removed: Optional[bool] = None
    task_status: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None
    await_: Optional[Dict[str, Any]] = Field(default=None, alias="await")
    abort: Optional[Dict[str, Any]] = None
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class SetTaskModelResponse(BaseModel):
    status: str
    model_provider: Optional[str] = None


class UpdateTaskEventConfigResponse(BaseModel):
    status: str
    event: str
    stage_type: str
    execution_profile: str
    stage_semantics: str
    config: Dict[str, Any]


class ModelTestRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider_id: str
    prompt: str = "hello"


class CreateAiCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    base_url: str
    api_key_env: Optional[str] = None
    api_key: Optional[str] = None


class UpdateAiCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key: Optional[str] = None


class CreateAiModelRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    credential_id: str
    name: str
    provider_type: str = "openai-compatible"
    model_kind: str = "llm"
    extra_config: Dict[str, Any] = Field(default_factory=dict)


class QuickCreateAiModelRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    provider_type: Optional[str] = None
    model_kind: str = "llm"
    extra_config: Dict[str, Any] = Field(default_factory=dict)


class UpdateAiModelRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    credential_id: Optional[str] = None
    name: Optional[str] = None
    provider_type: Optional[str] = None
    model_kind: Optional[str] = None
    extra_config: Optional[Dict[str, Any]] = None


class AiModelTestRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str = "ping"


class UpdateAiStageBindingsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    bindings: Dict[str, Optional[str]] = Field(default_factory=dict)


class SetCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    vector_model: Optional[str] = None
    rerank_model: Optional[str] = None
    notes: Optional[str] = None
    deleted_catalog_ids: Optional[List[str]] = None
    catalog: Optional[List[Dict[str, Any]]] = None
    bindings: Optional[List[Dict[str, Any]]] = None


class SetMcpServersRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    mcpServers: Optional[Dict[str, Any]] = None
    servers: Optional[Dict[str, Any]] = None


class SetSkillsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    notes: Optional[str] = None
    catalog: Optional[List[Dict[str, Any]]] = None


class SimpleStatusResponse(BaseModel):
    status: str


class ProviderTestResponse(BaseModel):
    output: str


class AiModelTestResponse(BaseModel):
    output: str
    provider: str


class AiStageBindingsResponse(BaseModel):
    bindings: Dict[str, Any]
    stages: List[str]
    execution_profiles: List[str]
