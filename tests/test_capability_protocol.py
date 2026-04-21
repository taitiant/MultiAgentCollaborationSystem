from orchestration.capabilities.adapters import build_adapter_manifest
from orchestration.capabilities.protocol import (
    PROTOCOL_VERSION,
    build_invocation_request,
    build_invocation_spec,
)
from orchestration.capabilities.registry import capability_prompt_view, merge_capability_settings


def test_build_invocation_spec_keeps_contract_and_binding_details():
    capability_def = {
        "id": "doc.write:v1",
        "label": "文档写入",
        "description": "输出 docx 文档",
        "handler": "doc.write:v1",
        "input_schema": {
            "type": "object",
            "required": ["content"],
            "properties": {
                "content": {"type": "string"},
                "target_filename": {"type": "string"},
            },
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "artifacts": {"type": "array"},
            },
        },
        "invocation_hint": "输入正文并输出 docx。",
        "supported_binding_types": ["http_api", "internal_tool"],
    }
    binding = {
        "id": "primary:doc.write:v1",
        "binding_type": "http_api",
        "transport": {
            "url": "http://doc.service/write",
            "method": "POST",
            "request_params": {"mode": "docx"},
        },
        "response_contract": {
            "status": "success",
            "artifact": {"filename": "documents/output.docx"},
        },
    }

    spec = build_invocation_spec(capability_def, binding=binding, options={"output_formats": ["docx"]}).as_dict()

    assert spec["protocol_version"] == PROTOCOL_VERSION
    assert spec["contract"]["input_schema"]["required"] == ["content"]
    assert spec["binding"]["transport"]["url"] == "http://doc.service/write"
    assert spec["binding"]["response_contract"]["status"] == "success"
    assert spec["options"]["output_formats"] == ["docx"]


def test_http_adapter_manifest_wraps_standard_request():
    spec = build_invocation_spec(
        {"id": "asset.generate:v1", "handler": "asset.generate:v1"},
        binding={
            "id": "asset-http",
            "binding_type": "http_api",
            "transport": {
                "url": "http://asset.service/generate",
                "method": "POST",
                "headers": {"Authorization": "Bearer token"},
                "request_params": {"workflow": "image-generation"},
            },
            "response_contract": {"status": "success"},
        },
    )
    request = build_invocation_request(
        task_id="task-1",
        stage_name="visual_assets",
        stage_type="assets",
        capability_id="asset.generate:v1",
        capability_input={"assets": [{"key": "mole"}]},
    )

    manifest = build_adapter_manifest(spec, request)

    assert manifest["binding_type"] == "http_api"
    assert manifest["endpoint"]["url"] == "http://asset.service/generate"
    assert manifest["request_params"]["workflow"] == "image-generation"
    assert manifest["response_contract"]["status"] == "success"
    assert manifest["request"]["capability_id"] == "asset.generate:v1"


def test_mcp_adapter_manifest_wraps_server_config():
    spec = build_invocation_spec(
        {"id": "doc.write:v1", "handler": "doc.write:v1"},
        binding={
            "id": "docx-mcp-write",
            "binding_type": "mcp_server",
            "mcp": {
                "server_name": "docx-mcp",
                "transport": "stdio",
                "launch": {
                    "command": "docx-mcp",
                    "args": ["--transport", "stdio"],
                },
                "tools": [
                    {"name": "create_document", "args_template": {"file_path": "{{target_filename}}"}},
                    {"name": "save_document", "args_template": {}},
                ],
            },
            "response_contract": {"status": "success", "artifact": {"filename": "documents/output.docx"}},
        },
    )
    request = build_invocation_request(
        task_id="task-2",
        stage_name="docs_delivery",
        stage_type="docs",
        capability_id="doc.write:v1",
        capability_input={"target_filename": "documents/output.docx", "content": "hello"},
    )

    manifest = build_adapter_manifest(spec, request)

    assert manifest["binding_type"] == "mcp_server"
    assert manifest["mcp"]["server_name"] == "docx-mcp"
    assert manifest["mcp"]["launch"]["command"] == "docx-mcp"
    assert manifest["mcp"]["tools"][0]["name"] == "create_document"
    assert manifest["response_contract"]["status"] == "success"


def test_capability_prompt_view_exposes_contract_summary():
    settings = merge_capability_settings()

    view = capability_prompt_view(settings)
    asset_entry = next(item for item in view if item["id"] == "asset.generate:v1")

    assert "assets" in asset_entry["input_fields"]
    assert "request_manifest" in asset_entry["output_fields"]
    assert "输入素材清单" in asset_entry["invocation_hint"]
