from __future__ import annotations

import json

import server.app as app_module


def test_set_skills_updates_catalog_and_preserves_notes(tmp_path, monkeypatch):
    config_path = tmp_path / "skills.json"
    monkeypatch.setattr(app_module, "SKILL_CONFIG_PATH", str(config_path))
    app_module.SKILL_CONFIG.clear()
    app_module.SKILL_CONFIG.update(
        {
            "notes": "skill-notes",
            "catalog": [],
        }
    )

    result = app_module.set_skills(
        {
            "catalog": [
                {
                    "id": "custom.skill:v1",
                    "label": "自定义技能",
                    "recommended_stage_types": ["coding"],
                    "default_stage_assignment": False,
                    "planner_visible": True,
                    "prompt_hint": "优先做最小修改",
                    "preferred_capabilities": ["code.edit:v1"],
                }
            ]
        }
    )

    assert result["notes"] == "skill-notes"
    assert any(item["id"] == "custom.skill:v1" for item in result["catalog"])
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert any(item["id"] == "custom.skill:v1" for item in saved["catalog"])
