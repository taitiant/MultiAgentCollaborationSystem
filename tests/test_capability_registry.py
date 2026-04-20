from orchestration.capability_registry import (
    get_capability_catalog,
    merge_capability_settings,
    capability_prompt_view,
)
from orchestration.stage_catalog import build_stage_type_blueprints


def test_merge_capability_settings_keeps_default_catalog():
    settings = merge_capability_settings({"notes": "demo"})

    capability_ids = [item["id"] for item in settings["catalog"]]

    assert settings["notes"] == "demo"
    assert "asset.generate:v1" in capability_ids
    assert "doc.read:v1" in capability_ids
    assert "doc.write:v1" in capability_ids
    assert "analysis.requirements:v1" not in capability_ids
    assert "design.arch:v1" not in capability_ids
    assert "code.edit:v1" not in capability_ids
    assert "planning.workflow:v1" not in capability_ids
    assert "planning.review:v1" not in capability_ids


def test_stage_blueprints_use_capability_catalog_defaults():
    catalog = get_capability_catalog()
    assert any(item["id"] == "asset.generate:v1" for item in catalog)

    blueprints = build_stage_type_blueprints({"catalog": catalog})

    assert blueprints["requirements"]["capabilities"] == []
    assert blueprints["assets"]["capabilities"] == []
    assert blueprints["coding"]["capabilities"] == []


def test_disabled_capability_is_hidden_from_defaults_and_planner_view():
    settings = merge_capability_settings(
        {
            "catalog": [
                {
                    "id": "asset.generate:v1",
                    "label": "素材生成",
                    "enabled": False,
                    "recommended_stage_types": ["assets"],
                    "default_stage_assignment": True,
                    "planner_visible": True,
                }
            ]
        }
    )

    blueprints = build_stage_type_blueprints(settings)
    prompt_view_ids = [item["id"] for item in capability_prompt_view(settings)]

    assert blueprints["assets"]["capabilities"] == []
    assert "asset.generate:v1" not in prompt_view_ids
