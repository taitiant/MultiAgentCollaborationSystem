from orchestration.capabilities.registry import (
    get_capability_catalog,
    merge_capability_settings,
    capability_prompt_view,
    sync_capability_settings_with_skills,
)
from orchestration.planning.stage_catalog import build_stage_type_blueprints


def test_merge_capability_settings_keeps_default_catalog():
    settings = merge_capability_settings({"notes": "demo"})

    capability_ids = [item["id"] for item in settings["catalog"]]
    capability_index = {item["id"]: item for item in settings["catalog"]}

    assert settings["notes"] == "demo"
    assert "asset.generate:v1" in capability_ids
    assert "doc.read:v1" in capability_ids
    assert "doc.write:v1" in capability_ids
    assert "analysis.requirements:v1" not in capability_ids
    assert "design.arch:v1" not in capability_ids
    assert "code.edit:v1" not in capability_ids
    assert "planning.workflow:v1" not in capability_ids
    assert "planning.review:v1" not in capability_ids
    assert capability_index["asset.generate:v1"]["source"] == "builtin"


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


def test_sync_capability_settings_with_skills_backfills_missing_capabilities():
    settings = sync_capability_settings_with_skills(
        {"catalog": []},
        {
            "catalog": [
                {
                    "id": "custom.skill:v1",
                    "label": "自定义技能",
                    "recommended_stage_types": ["coding"],
                    "preferred_capabilities": ["code.edit:v1"],
                    "required_capabilities": ["test.run:v1"],
                }
            ]
        },
    )

    capability_index = {item["id"]: item for item in settings["catalog"]}

    assert "code.edit:v1" in capability_index
    assert "test.run:v1" in capability_index
    assert capability_index["code.edit:v1"]["source"] == "skill_mapped"
    assert capability_index["code.edit:v1"]["category"] == "engineering"
    assert capability_index["code.edit:v1"]["recommended_stage_types"] == ["coding"]
    assert "internal_tool" in capability_index["code.edit:v1"]["supported_binding_types"]


def test_custom_capability_keeps_custom_source():
    settings = merge_capability_settings(
        {
            "catalog": [
                {
                    "id": "custom.export:v1",
                    "label": "自定义导出",
                }
            ]
        }
    )

    capability_index = {item["id"]: item for item in settings["catalog"]}

    assert capability_index["custom.export:v1"]["source"] == "custom"


def test_deleted_catalog_ids_hide_skill_backfilled_capabilities_but_not_builtin_ones():
    settings = sync_capability_settings_with_skills(
        {
            "deleted_catalog_ids": ["asset.generate:v1", "code.edit:v1"],
            "catalog": [],
        },
        {
            "catalog": [
                {
                    "id": "custom.skill:v1",
                    "recommended_stage_types": ["coding"],
                    "preferred_capabilities": ["code.edit:v1"],
                }
            ]
        },
    )

    capability_ids = [item["id"] for item in settings["catalog"]]

    assert "asset.generate:v1" in capability_ids
    assert "code.edit:v1" not in capability_ids
    assert settings["deleted_catalog_ids"] == ["asset.generate:v1", "code.edit:v1"]
