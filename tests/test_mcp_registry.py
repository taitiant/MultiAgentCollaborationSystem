from orchestration.mcp.registry import expand_mcp_binding, merge_mcp_settings, resolve_mcp_server


def test_merge_mcp_settings_accepts_claude_desktop_shape():
    settings = merge_mcp_settings(
        {
            "mcpServers": {
                "docx-mcp": {
                    "command": "uvx",
                    "args": ["docx-mcp"],
                },
                "ace-tool": {
                    "command": "npx",
                    "args": ["ace-tool", "--base-url", "http://example.com", "--token", "token-1"],
                },
            }
        }
    )

    assert settings["mcpServers"]["docx-mcp"]["command"] == "uvx"
    assert settings["mcpServers"]["docx-mcp"]["args"] == ["docx-mcp"]
    assert settings["mcpServers"]["ace-tool"]["command"] == "npx"


def test_expand_mcp_binding_merges_named_server_definition():
    settings = merge_mcp_settings(
        {
            "mcpServers": {
                "docx-mcp": {
                    "command": "uvx",
                    "args": ["docx-mcp"],
                    "env": {"PYTHONUTF8": "1"},
                }
            }
        }
    )

    expanded = expand_mcp_binding(
        {
            "server_name": "docx-mcp",
            "tools": [{"name": "create_document", "args_template": {"file_path": "{{target_filename}}"}}],
        },
        settings,
    )

    assert expanded["launch"]["command"] == "uvx"
    assert expanded["launch"]["args"] == ["docx-mcp"]
    assert expanded["launch"]["env"]["PYTHONUTF8"] == "1"
    assert expanded["tools"][0]["name"] == "create_document"
    assert resolve_mcp_server("docx-mcp", settings)["command"] == "uvx"
