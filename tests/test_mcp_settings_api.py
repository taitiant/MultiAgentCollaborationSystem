from __future__ import annotations

import json

import server.app as app_module


def test_set_mcp_servers_accepts_claude_desktop_payload(tmp_path, monkeypatch):
    config_path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(app_module, "MCP_CONFIG_PATH", str(config_path))
    app_module.MCP_CONFIG.clear()
    app_module.MCP_CONFIG.update({"mcpServers": {}})

    result = app_module.set_mcp_servers(
        {
            "mcpServers": {
                "docx-mcp": {
                    "command": "uvx",
                    "args": ["docx-mcp"],
                }
            }
        }
    )

    assert result["mcpServers"]["docx-mcp"]["command"] == "uvx"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["mcpServers"]["docx-mcp"]["args"] == ["docx-mcp"]
