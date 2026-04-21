from orchestration.capabilities.bindings import (
    get_capability_bindings_for_capability,
    merge_capability_bindings,
    resolve_capability_binding,
)
from orchestration.capabilities.registry import merge_capability_settings


def test_merge_capability_bindings_normalizes_entries():
    bindings = merge_capability_bindings(
        [
            {
                "id": "asset-http",
                "capability_id": "asset.generate:v1",
                "binding_type": "http_api",
                "transport": {
                    "url": "http://example.com/generate",
                    "method": "POST",
                    "request_params": {"workflow": "image-generation"},
                },
                "response_contract": {"status": "success", "assets": []},
                "defaults": {"asset_mode": "external_binding"},
            }
        ]
    )

    assert bindings[0]["id"] == "asset-http"
    assert bindings[0]["binding_type"] == "http_api"
    assert bindings[0]["transport"]["url"] == "http://example.com/generate"
    assert bindings[0]["transport"]["request_params"]["workflow"] == "image-generation"
    assert bindings[0]["response_contract"]["status"] == "success"


def test_resolve_capability_binding_prefers_lowest_priority():
    settings = merge_capability_settings(
        {
            "bindings": [
                {"id": "asset-b", "capability_id": "asset.generate:v1", "binding_type": "workflow_api", "priority": 20},
                {"id": "asset-a", "capability_id": "asset.generate:v1", "binding_type": "direct_model", "priority": 10},
            ]
        }
    )

    resolved = resolve_capability_binding("asset.generate:v1", settings)

    assert resolved is not None
    assert resolved["id"] == "asset-a"
    assert resolved["binding_type"] == "direct_model"


def test_get_capability_bindings_filters_disabled_entries():
    settings = merge_capability_settings(
        {
            "bindings": [
                {"id": "asset-off", "capability_id": "asset.generate:v1", "enabled": False},
                {"id": "asset-on", "capability_id": "asset.generate:v1", "enabled": True},
            ]
        }
    )

    bindings = get_capability_bindings_for_capability("asset.generate:v1", settings)

    assert [item["id"] for item in bindings] == ["asset-on"]


def test_merge_capability_bindings_normalizes_mcp_binding():
    bindings = merge_capability_bindings(
        [
            {
                "id": "docx-mcp-write",
                "capability_id": "doc.write:v1",
                "binding_type": "mcp_server",
                "mcp": {
                    "server_name": "docx-mcp",
                    "transport": "stdio",
                    "launch": {
                        "command": "docx-mcp",
                        "args": ["--transport", "stdio"],
                        "env": {"PYTHONUTF8": "1"},
                    },
                    "tools": [
                        {"name": "create_document", "args_template": {"file_path": "{{target_filename}}"}},
                        {"name": "save_document"},
                    ],
                },
                "defaults": {"prefer_external_binding": True},
            }
        ]
    )

    assert bindings[0]["binding_type"] == "mcp_server"
    assert bindings[0]["mcp"]["server_name"] == "docx-mcp"
    assert bindings[0]["mcp"]["launch"]["command"] == "docx-mcp"
    assert bindings[0]["mcp"]["tools"][0]["name"] == "create_document"
