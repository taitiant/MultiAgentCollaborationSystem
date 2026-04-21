from __future__ import annotations

import json

import server.app as app_module


def test_set_capabilities_updates_bindings_without_overwriting_knowledge_fields(tmp_path, monkeypatch):
    config_path = tmp_path / "capabilities.json"
    monkeypatch.setattr(app_module, "CAPA_CONFIG_PATH", str(config_path))
    app_module.CAPA_CONFIG.clear()
    app_module.CAPA_CONFIG.update(
        {
            "vector_model": "vec-1",
            "rerank_model": "rerank-1",
            "notes": "kb-notes",
            "catalog": [],
            "bindings": [],
        }
    )

    result = app_module.set_capabilities(
        {
            "bindings": [
                {
                    "id": "primary:asset.generate:v1",
                    "capability_id": "asset.generate:v1",
                    "binding_type": "http_api",
                    "transport": {"url": "http://asset.service/generate", "method": "POST"},
                }
            ]
        }
    )

    assert result["vector_model"] == "vec-1"
    assert result["rerank_model"] == "rerank-1"
    assert result["notes"] == "kb-notes"
    assert result["bindings"][0]["binding_type"] == "http_api"
    assert isinstance(result["default_catalog"], list)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["bindings"][0]["capability_id"] == "asset.generate:v1"


def test_set_capabilities_updates_knowledge_fields_without_overwriting_bindings(tmp_path, monkeypatch):
    config_path = tmp_path / "capabilities.json"
    monkeypatch.setattr(app_module, "CAPA_CONFIG_PATH", str(config_path))
    app_module.CAPA_CONFIG.clear()
    app_module.CAPA_CONFIG.update(
        {
            "vector_model": "",
            "rerank_model": "",
            "notes": "",
            "catalog": [],
            "bindings": [
                {
                    "id": "primary:doc.write:v1",
                    "capability_id": "doc.write:v1",
                    "binding_type": "internal_tool",
                    "tool": {"command": "python scripts/export_doc.py"},
                }
            ],
        }
    )

    result = app_module.set_capabilities(
        {
            "vector_model": "vec-2",
            "rerank_model": "rerank-2",
            "notes": "updated-notes",
        }
    )

    assert result["vector_model"] == "vec-2"
    assert result["rerank_model"] == "rerank-2"
    assert result["notes"] == "updated-notes"
    assert result["bindings"][0]["binding_type"] == "internal_tool"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["bindings"][0]["tool"]["command"] == "python scripts/export_doc.py"


def test_set_capabilities_preserves_builtin_capability_even_with_deleted_catalog_ids(tmp_path, monkeypatch):
    config_path = tmp_path / "capabilities.json"
    monkeypatch.setattr(app_module, "CAPA_CONFIG_PATH", str(config_path))
    app_module.CAPA_CONFIG.clear()
    app_module.CAPA_CONFIG.update(
        {
            "vector_model": "",
            "rerank_model": "",
            "notes": "",
            "deleted_catalog_ids": [],
            "catalog": [],
            "bindings": [],
        }
    )

    result = app_module.set_capabilities(
        {
            "deleted_catalog_ids": ["asset.generate:v1"],
            "catalog": [],
            "bindings": [],
        }
    )

    assert "asset.generate:v1" in [item["id"] for item in result["catalog"]]
    assert "asset.generate:v1" in result["deleted_catalog_ids"]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["deleted_catalog_ids"] == ["asset.generate:v1"]
