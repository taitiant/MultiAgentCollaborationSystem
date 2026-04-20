import json

from core import SystemState, Task
from domains.software_dev.agents.asset_agent import AssetAgent, needs_visual_assets
from orchestration.graph_builder import _build_fallback_plan


class FakeModel:
    def generate(self, prompt, context=None):
        return json.dumps(
            {
                "style": "cartoon-flat",
                "assets": [
                    {
                        "key": "mole",
                        "title": "地鼠",
                        "kind": "mole",
                        "purpose": "主角色",
                        "width": 200,
                        "height": 200,
                        "primary_color": "#8d5a3b",
                        "accent_color": "#f2c9a5",
                        "label": "Mole",
                        "prompt": "cartoon mole game sprite",
                    },
                    {
                        "key": "hole",
                        "title": "地洞",
                        "kind": "hole",
                        "purpose": "出生点",
                        "width": 240,
                        "height": 160,
                        "primary_color": "#5c4033",
                        "accent_color": "#2d2019",
                        "label": "Hole",
                        "prompt": "cartoon dirt hole game asset",
                    },
                ],
            },
            ensure_ascii=False,
        )


def test_needs_visual_assets_detects_image_driven_task():
    assert needs_visual_assets("开发一个打地鼠小游戏，需要地鼠、地洞和锤子图片素材")
    assert not needs_visual_assets("写一个命令行日志清理脚本")


def test_fallback_plan_inserts_assets_stage_for_visual_task():
    plan = _build_fallback_plan("开发一个打地鼠小游戏，需要地鼠、地洞和锤子图片素材")
    stage_types = [stage["stage_type"] for stage in plan["stages"]]

    assert "assets" in stage_types
    assert stage_types.index("assets") < stage_types.index("coding")


def test_asset_agent_generates_manifest_prompts_and_svg_files(tmp_path):
    workspace = tmp_path / "task-assets"
    (workspace / "analysis").mkdir(parents=True)
    (workspace / "design").mkdir(parents=True)
    (workspace / "analysis" / "requirements.md").write_text("需要地鼠、地洞和锤子素材。", encoding="utf-8")
    (workspace / "design" / "architecture.md").write_text("前端小游戏，将引用 assets/generated/*.svg。", encoding="utf-8")

    task = Task(
        task_id="task-assets",
        domain="software",
        required_capabilities=["asset.generate:v1"],
        context={"spec": "开发一个打地鼠小游戏，需要地鼠、地洞和锤子图片素材"},
        workspace_path=str(workspace),
    )

    message = AssetAgent(FakeModel(), stage_name="visual_assets", stage_type="assets").act(task, SystemState())
    filenames = [artifact.get("filename") for artifact in message.artifacts]

    assert "design/assets.md" in filenames
    assert "assets/manifest.json" in filenames
    assert "assets/generated/mole.svg" in filenames
    assert "assets/generated/hole.svg" in filenames
    assert "assets/prompts/mole.txt" in filenames

    manifest_artifact = next(artifact for artifact in message.artifacts if artifact.get("filename") == "assets/manifest.json")
    manifest = json.loads(manifest_artifact["content"])

    assert manifest["future_image_generation_ready"] is True
    assert manifest["render_mode"] == "svg_placeholder"
    assert len(manifest["assets"]) == 2

