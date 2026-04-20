import json
from pathlib import Path

import orchestration.graph_builder as graph_builder_module
from core import Task
from adapters.model_registry import ModelRegistry
from domains.software_dev.agents.patch_agent import PatchAgent
from orchestration.graph_builder import (
    ArchAgent,
    DocAgent,
    GraphBuilder,
    ReqAgent,
    TEXT_OUTPUT_QUALITY_GUARDRAIL,
    _extract_architecture_file_list,
    _extract_file_paths_from_lines,
    _normalize_architecture_markdown,
    _architecture_validation_issues,
    _docs_validation_issues,
    _build_rework_guidance,
    _infer_declared_stack,
    resolve_conversation_groups,
    write_leader_plan_snapshot,
)
from orchestration.workflow_plan import _normalize_stage_plan


def test_build_rework_guidance_for_coding_emphasizes_alignment_and_deduplication():
    feedback = (
        "main.py 导入了不存在的模块，同时 game.py 与 constants.py 出现两套重复规则定义，"
        "导致结构一致性不满足。"
    )

    guidance = _build_rework_guidance("coding", feedback, attempt=1)

    assert "architecture.md 的文件清单" in guidance
    assert "保留单一事实来源" in guidance
    assert "导入路径" in guidance or "调用方" in guidance
    assert "第 2 次评审返工" in guidance


def test_build_rework_guidance_for_smoke_failure_emphasizes_contract_alignment():
    feedback = "pytest smoke failed with KeyError: 'id' because assert player['id'] == player_id did not match runtime data."

    guidance = _build_rework_guidance("coding", feedback)

    assert "公开接口契约" in guidance
    assert "字段名" in guidance
    assert "不要通过修改测试" in guidance


def test_resolve_conversation_groups_prefers_explicit_planner_groups():
    stages = [
        {"name": "requirements", "stage_type": "requirements", "label": "需求分析"},
        {"name": "core_impl", "stage_type": "coding", "label": "核心实现"},
        {"name": "qa_verification", "stage_type": "testing", "label": "验证测试"},
    ]

    groups = resolve_conversation_groups(
        stages,
        [
            {
                "key": "delivery_loop",
                "label": "交付闭环",
                "kind": "loop",
                "stages": ["core_impl", "qa_verification"],
            }
        ],
    )

    assert any(group["key"] == "delivery_loop" for group in groups)
    assert stages[1]["conversation_group"]["key"] == "delivery_loop"
    assert stages[2]["conversation_group"]["label"] == "交付闭环"


def test_resolve_conversation_groups_falls_back_to_dynamic_dev_loop():
    stages = [
        {"name": "requirements", "stage_type": "requirements", "label": "需求分析"},
        {"name": "architecture", "stage_type": "architecture", "label": "架构设计"},
        {"name": "impl_a", "stage_type": "coding", "label": "实现 A"},
        {"name": "verify_a", "stage_type": "testing", "label": "验证 A"},
        {"name": "docs", "stage_type": "docs", "label": "文档交付"},
    ]

    groups = resolve_conversation_groups(stages, None)

    loop_group = next(group for group in groups if group["key"].startswith("flow:"))
    assert loop_group["label"] == "开发闭环"
    assert loop_group["stage_names"] == ["impl_a", "verify_a"]


def test_write_leader_plan_snapshot_writes_json_file(tmp_path):
    task = Task(
        task_id="task-plan-file",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={},
        workspace_path=str(tmp_path / "custom-workspace"),
    )
    payload = {"summary": "动态流程", "conversation_groups": [{"key": "loop", "label": "开发闭环"}]}

    plan_path = write_leader_plan_snapshot(task, str(tmp_path), payload)

    assert plan_path.endswith("plan/leader_plan.json")


def test_normalize_stage_plan_keeps_dynamic_stage_instance_and_internal_profile():
    stages = _normalize_stage_plan(
        [
            {
                "name": "ppt_outline_design",
                "label": "PPT 提纲设计",
                "stage_semantics": "design",
                "execution_profile": "architecture",
            },
            {
                "name": "slides_delivery",
                "label": "PPT 交付整理",
                "stage_semantics": "delivery",
                "execution_profile": "docs",
            },
        ],
        "制作一份汇报 PPT",
    )

    assert stages[0]["name"] == "ppt_outline_design"
    assert stages[0]["stage_semantics"] == "design"
    assert stages[0]["execution_profile"] == "architecture"
    assert stages[1]["stage_semantics"] == "delivery"
    assert stages[1]["execution_profile"] == "docs"


def test_plan_workflow_prompt_mentions_explicit_capability_assignment(tmp_path, monkeypatch):
    task = Task(
        task_id="task-plan-capabilities",
        domain="software",
        required_capabilities=["asset.generate:v1", "doc.write:v1"],
        context={"spec": "生成带素材和交付文档的小游戏"},
        workspace_path=str(tmp_path / "workspace"),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())
    captured = {}

    class FakePlanner:
        def generate(self, prompt, context=None):
            captured["prompt"] = prompt
            return json.dumps(
                {
                    "complexity": "standard",
                    "reference_preset": "custom",
                    "summary": "demo",
                    "stages": [
                        {
                            "name": "requirements",
                            "stage_type": "requirements",
                            "label": "需求分析",
                            "role": "需求分析师",
                            "prompt_template": "需求",
                            "capabilities": ["analysis.requirements:v1"],
                            "acceptance_criteria": "清晰",
                            "depends_on": [],
                            "human_checkpoint": False,
                        }
                    ],
                },
                ensure_ascii=False,
            )

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: FakePlanner())

    builder.plan_workflow(task, {"stages": []})

    prompt = captured["prompt"]
    plan_path = Path(task.workspace_path) / "plan" / "leader_plan.json"
    assert plan_path.exists()
    assert "Skill 目录" in prompt
    assert "skills 字段优先从 skill 目录中选择" in prompt
    assert "skill 不等于 capability" in prompt
    assert "如果某阶段需要主动调用特殊能力" in prompt
    assert "input_fields / output_fields / supported_binding_types" in prompt
    assert "不要留空" in prompt
    with open(plan_path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["summary"] == "demo"
    assert saved["stages"][0]["skills"] == ["requirements.discovery:v1"]


def test_human_checkpoint_stops_graph_before_downstream_stage(tmp_path, monkeypatch):
    task = Task(
        task_id="task-human-checkpoint",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(tmp_path / "workspace"),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())

    def should_not_execute(*_args, **_kwargs):
        raise AssertionError("downstream stage should not run while awaiting human checkpoint")

    monkeypatch.setattr(builder, "_select_model", should_not_execute)

    graph = builder.build(
        task,
        {
            "stages": [
                {"name": "requirements_gate", "stage_type": "requirements", "human_checkpoint": True},
                {"name": "delivery_docs", "stage_type": "docs", "human_checkpoint": False},
            ]
        },
    )

    result = graph.invoke({"task": task, "artifacts": []})

    assert result["await"]["stage"] == "requirements_gate"
    assert result["await"]["stage_type"] == "requirements"


def test_failed_architecture_review_blocks_downstream_stage_by_default(tmp_path, monkeypatch):
    task = Task(
        task_id="task-arch-review-block",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(tmp_path / "workspace"),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())
    lifecycle = []

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: object())

    def fake_req_act(self, task_obj, _state):
        return {"type": "md", "filename": "requirements.md", "content": "# requirements"}

    def fake_arch_act(self, task_obj, _state):
        return {"type": "md", "filename": "architecture.md", "content": "## 文件清单\nindex.html\nsrc/main.js"}

    def should_not_run_coding(self, task_obj, _state):
        raise AssertionError("coding stage should remain blocked after failed architecture review")

    def fake_review(_task_obj, stage_name, _payload, **_kwargs):
        return {
            "review_status": "ok",
            "pass": stage_name != "architecture_gate",
            "feedback": "architecture review failed" if stage_name == "architecture_gate" else "",
        }

    monkeypatch.setattr(ReqAgent, "act", fake_req_act)
    monkeypatch.setattr(ArchAgent, "act", fake_arch_act)
    monkeypatch.setattr(PatchAgent, "act", should_not_run_coding)
    monkeypatch.setattr(builder, "_review_stage_output", fake_review)

    graph = builder.build(
        task,
        {
            "stages": [
                {"name": "requirements_gate", "stage_type": "requirements"},
                {"name": "architecture_gate", "stage_type": "architecture"},
                {"name": "impl_gate", "stage_type": "coding"},
            ]
        },
        stage_logger=lambda stage, kind, payload: lifecycle.append((stage, kind, payload)),
    )

    result = graph.invoke({"task": task, "artifacts": []})

    assert result["error"] == "stage_review_failed:architecture_gate"
    assert ("impl_gate", "start") not in [(stage, kind) for stage, kind, _ in lifecycle]
    assert any(stage == "architecture_gate" and kind == "rework" for stage, kind, _ in lifecycle)


def test_architecture_review_can_be_explicitly_marked_non_blocking(tmp_path, monkeypatch):
    task = Task(
        task_id="task-arch-review-override",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "demo", "event_configs": {"architecture_gate": {"review_blocking": False}}},
        workspace_path=str(tmp_path / "workspace"),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())
    lifecycle = []

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: object())

    def fake_req_act(self, task_obj, _state):
        return {"type": "md", "filename": "requirements.md", "content": "# requirements"}

    def fake_arch_act(self, task_obj, _state):
        return {"type": "md", "filename": "architecture.md", "content": "## 文件清单\nindex.html\nsrc/main.js"}

    def fake_code_act(self, task_obj, _state):
        return {"type": "code", "filename": "src/main.js", "content": "export const ok = true;\n"}

    def fake_review(_task_obj, stage_name, _payload, **_kwargs):
        return {
            "review_status": "ok",
            "pass": stage_name != "architecture_gate",
            "feedback": "architecture review failed" if stage_name == "architecture_gate" else "",
        }

    monkeypatch.setattr(ReqAgent, "act", fake_req_act)
    monkeypatch.setattr(ArchAgent, "act", fake_arch_act)
    monkeypatch.setattr(PatchAgent, "act", fake_code_act)
    monkeypatch.setattr(builder, "_review_stage_output", fake_review)

    graph = builder.build(
        task,
        {
            "stages": [
                {"name": "requirements_gate", "stage_type": "requirements"},
                {"name": "architecture_gate", "stage_type": "architecture"},
                {"name": "impl_gate", "stage_type": "coding"},
            ]
        },
        stage_logger=lambda stage, kind, payload: lifecycle.append((stage, kind, payload)),
    )

    result = graph.invoke({"task": task, "artifacts": []})

    assert result.get("error") is None
    assert ("impl_gate", "start") in [(stage, kind) for stage, kind, _ in lifecycle]


def test_testing_review_fallback_accepts_web_manual_report(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "index.html").write_text("<!doctype html><script src='script.js'></script>", encoding="utf-8")
    (workspace / "style.css").write_text("body{margin:0;}\n", encoding="utf-8")
    (workspace / "script.js").write_text("console.log('ok')\n", encoding="utf-8")
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    report_path = tests_dir / "manual_test_report.md"
    report_path.write_text(
        "# 测试报告（自动回退）\n\n"
        "## 自动校验\n"
        "- 校验类型：静态资源校验\n"
        "- 执行命令：`web-static-check`\n"
        "- 退出码：0\n"
        "- 说明：已执行 Web 静态文件存在性与引用完整性校验。\n",
        encoding="utf-8",
    )
    task = Task(
        task_id="task-web-testing-fallback",
        domain="software",
        required_capabilities=["test.run:v1"],
        context={
            "spec": "编写一个 flappy bird 小游戏",
            "event_configs": {
                "game_testing": {
                    "stage_type": "testing",
                    "acceptance_criteria": "测试结论覆盖核心玩法和主要风险。",
                }
            },
        },
        workspace_path=str(workspace),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())

    def should_not_call_model(*_args, **_kwargs):
        raise AssertionError("web testing fallback should not require model review")

    monkeypatch.setattr(builder, "_select_model", should_not_call_model)

    review = builder._review_stage_output(
        task,
        "game_testing",
        {
            "artifacts": [
                {"type": "test_result", "uri": "inline", "content": {"command": "web-static-check", "exit_code": 0, "stdout": "ok", "stderr": ""}},
                {"type": "md", "uri": str(report_path), "mime": "text/markdown"},
            ]
        },
        stage_type="testing",
    )

    assert review["review_status"] == "fallback"
    assert review["pass"] is True
    assert "Web 静态校验" in review["feedback"]


def test_doc_agent_prompt_includes_quality_guardrail(tmp_path, monkeypatch):
    task = Task(
        task_id="task-doc-quality",
        domain="software",
        required_capabilities=["delivery.readme:v1"],
        context={"spec": "编写一个 flappy bird 小游戏"},
        workspace_path=str(tmp_path / "workspace"),
    )
    captured = {}

    class FakeModel:
        def generate(self, prompt, context=None):
            captured["prompt"] = prompt
            return "# README"

    agent = graph_builder_module.DocAgent(FakeModel())
    result = agent.act(task, None)

    assert result["filename"] == "docs/README.md"
    assert "输出前自检" in captured["prompt"]
    assert TEXT_OUTPUT_QUALITY_GUARDRAIL.strip() in captured["prompt"]


def test_arch_agent_normalizes_mixed_stack_file_lists(tmp_path):
    workspace = tmp_path / "workspace"
    analysis_dir = workspace / "analysis"
    analysis_dir.mkdir(parents=True)
    (analysis_dir / "requirements.md").write_text(
        "# requirements\n平台：浏览器 Web + Canvas，使用 TypeScript。\n",
        encoding="utf-8",
    )
    task = Task(
        task_id="task-arch-normalize",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "编写一个浏览器俄罗斯方块游戏"},
        workspace_path=str(workspace),
    )

    class FakeModel:
        def generate(self, _prompt, context=None):
            return (
                "# 技术方案\n"
                "采用 Web + Canvas + TypeScript。\n\n"
                "## 文件清单\n"
                "index.html\n"
                "src/main.ts\n"
                "src/game/board.ts\n\n"
                "## 文件清单\n"
                "requirements.txt\n"
                "code/main.py\n"
            )

    agent = ArchAgent(FakeModel(), stage_name="tech_architecture", stage_type="architecture")
    result = agent.act(task, None)
    content = result["content"]

    assert content.count("## 文件清单") == 1
    assert "code/index.html" in content
    assert "code/src/main.ts" in content
    assert "requirements.txt" not in content
    assert "code/main.py" not in content


def test_extract_architecture_file_list_accepts_compatible_heading_typo():
    arch_text = (
        "# 技术方案\n\n"
        "## 文件单\n"
        "code/index.html\n"
        "code/js/main.js\n"
    )

    files = _extract_architecture_file_list(arch_text)

    assert files == ["code/index.html", "code/js/main.js"]


def test_normalize_architecture_markdown_upgrades_compatible_heading_to_standard():
    arch_text = (
        "# 技术方案\n\n"
        "## 文件单\n"
        "code/index.html\n"
        "code/js/main.js\n"
    )

    normalized = _normalize_architecture_markdown(
        "编写一个 flappybird 游戏",
        "运行环境：浏览器 Web + Canvas。",
        arch_text,
    )

    assert "## 文件单" not in normalized
    assert normalized.count("## 文件清单") == 1
    assert "code/index.html" in normalized
    assert "code/js/main.js" in normalized


def test_extract_file_paths_from_lines_keeps_root_level_web_files_before_normalization():
    files = _extract_file_paths_from_lines([
        "index.html",
        "style.css",
        "script.js",
    ])

    assert files == ["index.html", "style.css", "script.js"]


def test_normalize_architecture_markdown_moves_web_files_under_code_dir():
    arch_text = (
        "# 技术方案\n\n"
        "## 文件清单\n"
        "index.html\n"
        "style.css\n"
        "script.js\n"
        "src/main.ts\n"
    )

    normalized = _normalize_architecture_markdown(
        "编写一个 flappy bird 小游戏",
        "运行环境：浏览器 Web + Canvas。",
        arch_text,
    )

    assert "code/index.html" in normalized
    assert "code/style.css" in normalized
    assert "code/script.js" in normalized
    assert "code/src/main.ts" in normalized


def test_architecture_validation_detects_mixed_stack_files():
    arch_text = (
        "# 技术方案\n\n"
        "## 文件清单\n"
        "index.html\n"
        "src/main.ts\n"
        "requirements.txt\n"
        "code/main.py\n"
    )

    issues = _architecture_validation_issues(
        "编写一个浏览器俄罗斯方块游戏",
        "平台：浏览器 Web + Canvas",
        arch_text,
    )

    assert any("混入了 Web/TS 与 Python" in issue for issue in issues)
    assert any("Web 交付" in issue for issue in issues)


def test_architecture_validation_detects_requirement_stack_conflict():
    arch_text = (
        "# 技术方案\n\n"
        "采用原生 Web + Canvas。\n\n"
        "## 文件清单\n"
        "index.html\n"
        "src/main.js\n"
    )

    issues = _architecture_validation_issues(
        "编写一个 flappy bird 小游戏",
        "运行环境默认：Python + Pygame 桌面小游戏。",
        arch_text,
    )

    assert _infer_declared_stack("运行环境默认：Python + Pygame 桌面小游戏。") == "python"
    assert any("未遵循上游需求已给定的默认技术栈" in issue for issue in issues)


def test_architecture_review_fallback_rejects_deterministically_invalid_doc(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    design_dir = workspace / "design"
    analysis_dir = workspace / "analysis"
    design_dir.mkdir(parents=True)
    analysis_dir.mkdir(parents=True)
    (analysis_dir / "requirements.md").write_text("平台：浏览器 Web + Canvas\n", encoding="utf-8")
    arch_path = design_dir / "architecture.md"
    arch_path.write_text(
        "# 技术方案\n\n## 文件清单\nindex.html\nsrc/main.ts\nrequirements.txt\ncode/main.py\n",
        encoding="utf-8",
    )
    task = Task(
        task_id="task-arch-review-fallback",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "编写一个浏览器俄罗斯方块游戏", "event_configs": {"architecture": {"acceptance_criteria": "文件清单唯一且可直接用于编码"}}},
        workspace_path=str(workspace),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())

    class FakeReviewer:
        def generate(self, _prompt, context=None):
            return "not-json"

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: FakeReviewer())

    review = builder._review_stage_output(
        task,
        "tech_architecture",
        {"artifacts": [{"uri": str(arch_path), "type": "md"}], "output_summary": {"artifact_count": 1}},
        stage_type="architecture",
    )

    assert review["pass"] is False
    assert "结构问题" in review["feedback"] or "混入" in review["feedback"]


def test_docs_validation_detects_missing_required_sections():
    issues = _docs_validation_issues(
        "# README\n\n"
        "## 项目简介\n"
        "这是一个小游戏。\n"
    )

    assert any("运行方式" in issue for issue in issues)
    assert any("文件结构" in issue for issue in issues)
    assert any("限制说明" in issue for issue in issues)
    assert any("测试结论" in issue for issue in issues)


def test_docs_review_accepts_when_readme_sections_exist_even_if_model_says_evidence_insufficient(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    docs_dir = workspace / "docs"
    docs_dir.mkdir(parents=True)
    readme_path = docs_dir / "README.md"
    readme_path.write_text(
        "# README\n\n"
        "## 启动方式\n"
        "python code/main.py\n\n"
        "## 文件结构\n"
        "code/main.py\n\n"
        "## 已知限制\n"
        "需要 pygame。\n\n"
        "## 测试/验证结论\n"
        "已完成编译校验。\n",
        encoding="utf-8",
    )
    task = Task(
        task_id="task-doc-review-rescue",
        domain="software",
        required_capabilities=["delivery.readme:v1"],
        context={"spec": "demo", "event_configs": {"docs": {"acceptance_criteria": "README 清晰说明运行方式、文件结构、限制与测试结论。"}}},
        workspace_path=str(workspace),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())

    class FakeReviewer:
        def generate(self, _prompt, context=None):
            return json.dumps(
                {
                    "pass": False,
                    "score": 0.61,
                    "feedback": "基于当前可见证据，尚不能确认 README 是否完整覆盖文件结构、限制说明与测试结论。",
                    "risks": ["证据不足"],
                    "next_actions": ["补充更完整预览"],
                },
                ensure_ascii=False,
            )

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: FakeReviewer())

    review = builder._review_stage_output(
        task,
        "release_docs",
        {"artifacts": [{"uri": str(readme_path), "type": "md"}], "output_summary": {"artifact_count": 1}},
        stage_type="docs",
    )

    assert review["pass"] is True
    assert "自动纠正为通过" in review["feedback"]


def test_coding_review_rework_triggers_orphan_cleanup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    task = Task(
        task_id="task-coding-rework-cleanup",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(workspace),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())
    review_calls = {"count": 0}
    cleanup_calls = []

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: object())

    def fake_code_act(self, task_obj, _state):
        return {"type": "code", "filename": "code/main.py", "content": "print('ok')\n"}

    def fake_review(_task_obj, stage_name, _payload, **_kwargs):
        review_calls["count"] += 1
        return {
            "review_status": "ok",
            "pass": review_calls["count"] > 1,
            "feedback": "need rework" if review_calls["count"] == 1 else "",
        }

    def fake_cleanup(task_obj, base_dir, stage_name, stage_def=None):
        cleanup_calls.append((task_obj.task_id, base_dir, stage_name, dict(stage_def or {})))
        return 2

    monkeypatch.setattr(PatchAgent, "act", fake_code_act)
    monkeypatch.setattr(builder, "_review_stage_output", fake_review)
    monkeypatch.setattr(graph_builder_module, "cleanup_architecture_orphan_files", fake_cleanup)

    graph = builder.build(
        task,
        {"stages": [{"name": "core_impl", "stage_type": "coding"}]},
    )

    result = graph.invoke({"task": task, "artifacts": []})

    assert result.get("error") is None
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][2] == "core_impl"
    assert cleanup_calls[0][3]["stage_type"] == "coding"


def test_stage_submission_persists_decision_memory_status(tmp_path, monkeypatch):
    task = Task(
        task_id="task-decision-memory",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(tmp_path / "workspace"),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())
    blackboard_updates = []

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: object())

    def fake_req_act(self, task_obj, _state):
        return {"type": "md", "filename": "requirements.md", "content": "# requirements"}

    review_results = [
        {
            "review_status": "ok",
            "pass": True,
            "feedback": "需求范围已收敛。",
            "next_actions": ["后续阶段按该范围执行"],
        },
        {
            "review_status": "ok",
            "pass": False,
            "feedback": "实现细节仍需返工。",
        },
        {
            "review_status": "ok",
            "pass": False,
            "feedback": "实现细节仍需返工。",
        },
    ]
    review_calls = {"count": 0}

    original_upsert = graph_builder_module.CollaborationHub.upsert_blackboard

    def capture_upsert(self, **kwargs):
        blackboard_updates.append(kwargs)
        return original_upsert(self, **kwargs)

    def fake_review_output(*_args, **_kwargs):
        index = min(review_calls["count"], len(review_results) - 1)
        review_calls["count"] += 1
        return review_results[index]

    monkeypatch.setattr(ReqAgent, "act", fake_req_act)
    monkeypatch.setattr(builder, "_review_stage_output", fake_review_output)
    monkeypatch.setattr(graph_builder_module.CollaborationHub, "upsert_blackboard", capture_upsert)

    graph = builder.build(
        task,
        {
            "stages": [
                {"name": "requirements_gate", "stage_type": "requirements"},
                {"name": "implementation_gate", "stage_type": "requirements", "human_checkpoint": False},
            ]
        },
    )

    result = graph.invoke({"task": task, "artifacts": []})

    assert result["error"] == "stage_review_failed:implementation_gate"
    decision_updates = [item for item in blackboard_updates if item.get("entry_type") == "decision_memory"]
    assert len(decision_updates) == 3
    assert decision_updates[0]["payload"]["pass"] is True
    assert "需求范围已收敛" in decision_updates[0]["content"]
    assert decision_updates[1]["payload"]["pass"] is False
    assert "原结论暂不固化" in decision_updates[1]["content"]
    assert decision_updates[2]["payload"]["pass"] is False


def test_human_decision_request_awaits_user_before_rework(tmp_path, monkeypatch):
    task = Task(
        task_id="task-human-decision",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(tmp_path / "workspace"),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())
    lifecycle = []

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: object())

    def fake_req_act(self, task_obj, _state):
        return {"type": "md", "filename": "requirements.md", "content": "# requirements"}

    def fake_review(_task_obj, _stage_name, _payload, **_kwargs):
        return {
            "review_status": "ok",
            "pass": False,
            "feedback": "这里有两套都可行的范围方案，需要用户拍板。",
            "human_decision_required": True,
            "decision_question": "优先做桌面版还是浏览器版？",
            "decision_options": ["桌面版（Pygame）", "浏览器版（Canvas）"],
            "decision_reason": "两套方案都可落地，但技术路线会直接影响后续文件结构。",
        }

    monkeypatch.setattr(ReqAgent, "act", fake_req_act)
    monkeypatch.setattr(builder, "_review_stage_output", fake_review)

    graph = builder.build(
        task,
        {"stages": [{"name": "requirements_gate", "stage_type": "requirements"}]},
        stage_logger=lambda stage, kind, payload: lifecycle.append((stage, kind, payload)),
    )

    result = graph.invoke({"task": task, "artifacts": []})

    assert result.get("error") is None
    assert result["await"]["kind"] == "human_decision"
    assert result["await"]["question"] == "优先做桌面版还是浏览器版？"
    assert task.context["pending_human_decision"]["options"] == ["桌面版（Pygame）", "浏览器版（Canvas）"]
    assert any(stage == "requirements_gate" and kind == "await" for stage, kind, _ in lifecycle)


def test_non_agent_stage_payload_uses_current_stage_artifacts_only(tmp_path, monkeypatch):
    task = Task(
        task_id="task-stage-artifact-scope",
        domain="software",
        required_capabilities=["delivery.readme:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(tmp_path / "workspace"),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())
    lifecycle = []

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: object())

    def fake_req_act(self, task_obj, _state):
        return {"type": "md", "filename": "analysis/requirements.md", "content": "# requirements"}

    def fake_doc_act(self, task_obj, _state):
        return {"type": "md", "filename": "docs/README.md", "content": "# readme"}

    def fake_review(*_args, **_kwargs):
        return {"review_status": "skipped", "pass": None, "feedback": "skip"}

    monkeypatch.setattr(ReqAgent, "act", fake_req_act)
    monkeypatch.setattr(DocAgent, "act", fake_doc_act)
    monkeypatch.setattr(builder, "_review_stage_output", fake_review)

    graph = builder.build(
        task,
        {
            "stages": [
                {"name": "requirements_gate", "stage_type": "requirements"},
                {"name": "release_docs", "stage_type": "docs"},
            ]
        },
        stage_logger=lambda stage, kind, payload: lifecycle.append((stage, kind, payload)),
    )

    result = graph.invoke({"task": task, "artifacts": []})

    assert result.get("error") is None
    assert len(result.get("artifacts") or []) == 2

    done_payloads = {
        stage: payload
        for stage, kind, payload in lifecycle
        if kind == "done"
    }
    requirements_payload = done_payloads["requirements_gate"]
    docs_payload = done_payloads["release_docs"]

    assert requirements_payload["output_summary"]["artifact_count"] == 1
    assert len(requirements_payload["artifacts"]) == 1
    assert str(requirements_payload["artifacts"][0]["uri"]).endswith("analysis/requirements.md")

    assert docs_payload["output_summary"]["artifact_count"] == 1
    assert len(docs_payload["artifacts"]) == 1
    assert docs_payload["output_summary"]["artifact_types"] == ["md"]
    assert str(docs_payload["artifacts"][0]["uri"]).endswith("docs/README.md")


def test_stage_delivery_blackboard_summary_uses_current_stage_artifact_count_only(tmp_path, monkeypatch):
    task = Task(
        task_id="task-stage-delivery-summary-scope",
        domain="software",
        required_capabilities=["delivery.readme:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(tmp_path / "workspace"),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())
    blackboard_updates = []

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: object())

    def fake_req_act(self, task_obj, _state):
        return {"type": "md", "filename": "analysis/requirements.md", "content": "# requirements"}

    def fake_doc_act(self, task_obj, _state):
        return {"type": "md", "filename": "docs/README.md", "content": "# readme"}

    def fake_review(*_args, **_kwargs):
        return {"review_status": "skipped", "pass": None, "feedback": "skip"}

    original_upsert = graph_builder_module.CollaborationHub.upsert_blackboard

    def capture_upsert(self, **kwargs):
        blackboard_updates.append(kwargs)
        return original_upsert(self, **kwargs)

    monkeypatch.setattr(ReqAgent, "act", fake_req_act)
    monkeypatch.setattr(DocAgent, "act", fake_doc_act)
    monkeypatch.setattr(builder, "_review_stage_output", fake_review)
    monkeypatch.setattr(graph_builder_module.CollaborationHub, "upsert_blackboard", capture_upsert)

    graph = builder.build(
        task,
        {
            "stages": [
                {"name": "requirements_gate", "stage_type": "requirements"},
                {"name": "release_docs", "stage_type": "docs"},
            ]
        },
    )

    result = graph.invoke({"task": task, "artifacts": []})

    assert result.get("error") is None

    delivery_updates = [
        item for item in blackboard_updates
        if item.get("entry_type") == "stage_delivery"
    ]
    assert len(delivery_updates) == 2
    for update in delivery_updates:
        assert update["payload"]["output_summary"]["artifact_count"] == 1
        assert "产物数量：1。" in update["content"]


def test_stage_can_trigger_capability_invoke_without_polluting_readme(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    task = Task(
        task_id="task-capability-invoke-doc",
        domain="software",
        required_capabilities=["delivery.readme:v1", "doc.write:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(workspace),
    )
    builder = GraphBuilder(str(tmp_path), ModelRegistry())

    monkeypatch.setattr(builder, "_select_model", lambda *_args, **_kwargs: object())

    def fake_doc_act(self, task_obj, _state):
        return {
            "type": "md",
            "filename": "docs/README.md",
            "content": (
                "# README\n\n"
                "这是交付说明。\n\n"
                "```capability.invoke\n"
                "{\"capability_id\":\"doc.write:v1\",\"input\":{\"target_filename\":\"documents/guide.docx\",\"content\":\"# Guide\\n\\nHello\",\"output_formats\":[\"docx\"]}}\n"
                "```\n"
            ),
        }

    def fake_review(*_args, **_kwargs):
        return {"review_status": "skipped", "pass": None, "feedback": "skip"}

    monkeypatch.setattr(DocAgent, "act", fake_doc_act)
    monkeypatch.setattr(builder, "_review_stage_output", fake_review)

    graph = builder.build(
        task,
        {
            "stages": [
                {"name": "release_docs", "stage_type": "docs", "capabilities": []},
            ]
        },
    )

    result = graph.invoke({"task": task, "artifacts": []})

    assert result.get("error") is None
    artifact_uris = [str(item.get("uri") or "") for item in (result.get("artifacts") or [])]
    readme_uri = next(uri for uri in artifact_uris if uri.endswith("docs/README.md"))
    assert any(uri.endswith("documents/guide.docx") for uri in artifact_uris)

    readme_path = Path(readme_uri)
    readme_text = readme_path.read_text(encoding="utf-8")
    assert "capability.invoke" not in readme_text
