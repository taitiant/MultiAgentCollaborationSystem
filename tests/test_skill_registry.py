from orchestration.skill_registry import (
    build_skill_runtime_context,
    get_skill_catalog,
    merge_skill_settings,
    skill_prompt_view,
)
from orchestration.stage_catalog import build_stage_type_blueprints
from orchestration.stage_catalog import (
    default_execution_profile_for_semantics,
    normalize_stage_semantics,
    resolve_stage_execution_profile,
    stage_blueprint,
)


def test_merge_skill_settings_keeps_default_catalog():
    settings = merge_skill_settings({"notes": "skill-demo"})

    skill_ids = [item["id"] for item in settings["catalog"]]

    assert settings["notes"] == "skill-demo"
    assert "planning.dynamic_orchestration:v1" in skill_ids
    assert "planning.review_governance:v1" in skill_ids
    assert "requirements.discovery:v1" in skill_ids
    assert "coding.incremental_delivery:v1" in skill_ids


def test_stage_blueprints_use_skill_catalog_defaults():
    catalog = get_skill_catalog()
    blueprints = build_stage_type_blueprints(skill_settings={"catalog": catalog})

    assert blueprints["requirements"]["skills"] == ["requirements.discovery:v1"]
    assert blueprints["assets"]["skills"] == ["asset.prompting:v1"]
    assert blueprints["coding"]["skills"] == ["coding.incremental_delivery:v1"]


def test_disabled_skill_is_hidden_from_defaults_and_planner_view():
    settings = merge_skill_settings(
        {
            "catalog": [
                {
                    "id": "coding.incremental_delivery:v1",
                    "label": "增量开发与定点修补",
                    "enabled": False,
                    "recommended_stage_types": ["coding"],
                    "default_stage_assignment": True,
                    "planner_visible": True,
                }
            ]
        }
    )

    blueprints = build_stage_type_blueprints(skill_settings=settings)
    prompt_view_ids = [item["id"] for item in skill_prompt_view(settings)]

    assert blueprints["coding"]["skills"] == []
    assert "coding.incremental_delivery:v1" not in prompt_view_ids


def test_build_skill_runtime_context_explains_skill_capability_relationship():
    text = build_skill_runtime_context(["coding.incremental_delivery:v1"])

    assert "skill 不是 capability 的唯一入口" in text
    assert "增量开发与定点修补" in text


def test_stage_blueprint_exposes_execution_profile_and_semantics():
    blueprint = stage_blueprint("architecture")

    assert blueprint["stage_type"] == "architecture"
    assert blueprint["execution_profile"] == "architecture"
    assert blueprint["stage_semantics"] == "design"


def test_stage_semantics_can_map_to_default_execution_profile():
    assert normalize_stage_semantics("verification") == "verification"
    assert default_execution_profile_for_semantics("verification") == "testing"
    assert resolve_stage_execution_profile({"stage_semantics": "delivery"}) == "docs"
