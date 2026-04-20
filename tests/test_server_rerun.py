from core import Task
import server.app as app_module
from server.app import _cleanup_architecture_orphan_files, _cleanup_latest_stage_artifacts, _should_cleanup_stage_artifacts, task_conversation_groups, build_task_group_blackboards, run_single_stage


def test_should_cleanup_stage_artifacts_respects_stage_config_and_stage_type():
    task = Task(
        task_id="task-rerun-cleanup",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "leader_plan": {
                "stages": [
                    {"name": "core_impl", "stage_type": "coding"},
                    {"name": "qa_verification", "stage_type": "testing"},
                    {"name": "delivery_docs", "stage_type": "docs"},
                ]
            },
            "event_configs": {
                "core_impl": {"stage_type": "coding", "rework_cleanup": False},
            },
        },
    )

    assert _should_cleanup_stage_artifacts(task, "core_impl", {"stage_type": "coding"}) is False
    assert _should_cleanup_stage_artifacts(task, "qa_verification", {"stage_type": "testing"}) is False
    assert _should_cleanup_stage_artifacts(task, "delivery_docs", {"stage_type": "docs"}) is False


def test_cleanup_latest_stage_artifacts_docs_scope_does_not_delete_code_files(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "code").mkdir(parents=True)
    doc_path = workspace / "docs" / "README.md"
    code_path = workspace / "code" / "main.py"
    doc_path.write_text("doc\n", encoding="utf-8")
    code_path.write_text("print('ok')\n", encoding="utf-8")

    task = Task(
        task_id="task-doc-cleanup-scope",
        domain="software",
        required_capabilities=["delivery.readme:v1"],
        context={
            "leader_plan": {
                "stages": [
                    {"name": "delivery_docs", "stage_type": "docs"},
                ]
            }
        },
        workspace_path=str(workspace),
    )

    monkeypatch.setattr(
        app_module.db,
        "get_latest_stage_done_event",
        lambda *_args, **_kwargs: {
            "payload": {
                "stage": "delivery_docs",
                "artifacts": [
                    {"uri": str(doc_path), "type": "md"},
                    {"uri": str(code_path), "type": "code"},
                ]
            }
        },
    )

    removed = _cleanup_latest_stage_artifacts(task, "delivery_docs")

    assert removed == 1
    assert not doc_path.exists()
    assert code_path.exists()


def test_cleanup_architecture_orphan_files_removes_stale_project_files(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "analysis").mkdir(parents=True)
    (workspace / "design").mkdir(parents=True)
    (workspace / "code" / "src").mkdir(parents=True)
    (workspace / "docs").mkdir(parents=True)

    (workspace / "analysis" / "requirements.md").write_text("平台：浏览器 Web + Canvas\n", encoding="utf-8")
    (workspace / "design" / "architecture.md").write_text(
        "## 文件清单\ncode/index.html\ncode/src/main.ts\ncode/src/game/board.ts\ntests/game.test.ts\n",
        encoding="utf-8",
    )
    (workspace / "code" / "index.html").write_text("<html></html>", encoding="utf-8")
    (workspace / "code" / "src" / "main.ts").write_text("export {};\n", encoding="utf-8")
    (workspace / "code" / "src" / "old.ts").write_text("obsolete\n", encoding="utf-8")
    (workspace / "code" / "legacy.py").write_text("print('old')\n", encoding="utf-8")
    (workspace / "requirements.txt").write_text("pygame\n", encoding="utf-8")
    (workspace / "docs" / "README.md").write_text("keep\n", encoding="utf-8")

    task = Task(
        task_id="task-orphan-cleanup",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "编写一个浏览器俄罗斯方块游戏"},
        workspace_path=str(workspace),
    )

    removed = _cleanup_architecture_orphan_files(task, "core_impl", {"stage_type": "coding"})

    assert removed >= 3
    assert (workspace / "code" / "src" / "main.ts").exists()
    assert not (workspace / "code" / "src" / "old.ts").exists()
    assert not (workspace / "code" / "legacy.py").exists()
    assert not (workspace / "requirements.txt").exists()
    assert (workspace / "docs" / "README.md").exists()


def test_cleanup_architecture_orphan_files_skips_non_coding_stage(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "design").mkdir(parents=True)
    (workspace / "design" / "architecture.md").write_text("## 文件清单\nsrc/main.ts\n", encoding="utf-8")
    (workspace / "src").mkdir(parents=True)
    stale = workspace / "src" / "old.ts"
    stale.write_text("obsolete\n", encoding="utf-8")

    task = Task(
        task_id="task-orphan-cleanup-skip",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "编写一个浏览器俄罗斯方块游戏"},
        workspace_path=str(workspace),
    )

    removed = _cleanup_architecture_orphan_files(task, "tech_architecture", {"stage_type": "architecture"})

    assert removed == 0
    assert stale.exists()


def test_cleanup_architecture_orphan_files_normalizes_legacy_root_web_paths(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "analysis").mkdir(parents=True)
    (workspace / "design").mkdir(parents=True)
    (workspace / "analysis" / "requirements.md").write_text("平台：浏览器 Web + Canvas\n", encoding="utf-8")
    (workspace / "design" / "architecture.md").write_text(
        "## 文件清单\nindex.html\nstyle.css\nscript.js\n",
        encoding="utf-8",
    )
    (workspace / "code").mkdir(parents=True)
    (workspace / "code" / "index.html").write_text("<html></html>", encoding="utf-8")
    (workspace / "code" / "style.css").write_text("body{}\n", encoding="utf-8")
    (workspace / "code" / "script.js").write_text("console.log('ok')\n", encoding="utf-8")
    (workspace / "index.html").write_text("legacy\n", encoding="utf-8")

    task = Task(
        task_id="task-orphan-cleanup-legacy-web",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "编写一个 flappy bird 小游戏"},
        workspace_path=str(workspace),
    )

    removed = _cleanup_architecture_orphan_files(task, "core_impl", {"stage_type": "coding"})

    assert removed >= 1
    assert (workspace / "code" / "index.html").exists()
    assert (workspace / "code" / "style.css").exists()
    assert (workspace / "code" / "script.js").exists()
    assert not (workspace / "index.html").exists()


def test_task_conversation_groups_reads_leader_plan_groups():
    task = Task(
        task_id="task-conversation-groups",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "leader_plan": {
                "stages": [
                    {
                        "name": "core_impl",
                        "stage_type": "coding",
                        "label": "核心实现",
                        "conversation_group": {"key": "delivery_loop", "label": "交付闭环"},
                    },
                    {
                        "name": "qa_verification",
                        "stage_type": "testing",
                        "label": "验证测试",
                        "conversation_group": {"key": "delivery_loop", "label": "交付闭环"},
                    },
                ],
                "conversation_groups": [
                    {
                        "key": "delivery_loop",
                        "label": "交付闭环",
                        "kind": "loop",
                        "stage_names": ["core_impl", "qa_verification"],
                    }
                ],
            },
        },
    )

    groups = task_conversation_groups(task)

    assert groups == [
        {
            "key": "delivery_loop",
            "label": "交付闭环",
            "kind": "loop",
            "stage_names": ["core_impl", "qa_verification"],
        }
    ]


def test_build_task_group_blackboards_returns_group_level_snapshot():
    task = Task(
        task_id="task-group-blackboard",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "leader_plan": {
                "stages": [
                    {
                        "name": "core_impl",
                        "stage_type": "coding",
                        "label": "核心实现",
                        "conversation_group": {"key": "delivery_loop", "label": "交付闭环"},
                    },
                    {
                        "name": "qa_verification",
                        "stage_type": "testing",
                        "label": "验证测试",
                        "conversation_group": {"key": "delivery_loop", "label": "交付闭环"},
                    },
                ],
                "conversation_groups": [
                    {
                        "key": "delivery_loop",
                        "label": "交付闭环",
                        "kind": "loop",
                        "stage_names": ["core_impl", "qa_verification"],
                    }
                ],
            },
        },
    )

    group_blackboards = build_task_group_blackboards(
        task,
        messages=[
            {
                "message_id": "msg-1",
                "stage_name": "core_impl",
                "created_at": 10,
                "content": "已提交实现版本。",
            }
        ],
        blackboard=[
            {
                "entry_id": "entry-1",
                "entry_key": "stage:core_impl:rework",
                "entry_type": "rework_request",
                "title": "当前返工要求",
                "content": "请修复边界条件并补齐测试。",
                "stage_name": "core_impl",
                "payload": {},
                "updated_at": 20,
            }
        ],
    )

    assert len(group_blackboards) == 1
    assert group_blackboards[0]["key"] == "delivery_loop"
    assert group_blackboards[0]["status"] == "open"
    assert "请修复边界条件并补齐测试" in group_blackboards[0]["shared_context"]


def test_run_single_stage_returns_latest_stage_artifacts_only(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    task = Task(
        task_id="task-stage-rerun-artifact-scope",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "leader_plan": {
                "stages": [
                    {"name": "core_impl", "stage_type": "coding", "label": "核心开发"},
                ]
            },
            "event_configs": {},
        },
        workspace_path=str(workspace),
    )

    original_tasks = dict(app_module.state.tasks)
    original_status = dict(app_module.state.task_status)
    original_history = list(app_module.state.history)
    app_module.state.tasks[task.task_id] = task
    app_module.state.task_status[task.task_id] = "created"

    class FakeGraph:
        def invoke(self, _init_state):
            return {
                "artifacts": [
                    {"uri": str(workspace / "design" / "architecture.md"), "type": "md"},
                    {"uri": str(workspace / "code" / "main.py"), "type": "code"},
                ]
            }

    monkeypatch.setattr(app_module.graph_builder, "build", lambda *_args, **_kwargs: FakeGraph())
    monkeypatch.setattr(app_module, "latest_stage_runtime_outcome", lambda *_args, **_kwargs: {"status": "ok", "event_type": "StageDone", "payload": {}})
    monkeypatch.setattr(
        app_module.db,
        "get_latest_stage_done_event",
        lambda *_args, **_kwargs: {
            "payload": {
                "stage": "core_impl",
                "artifacts": [
                    {"uri": str(workspace / "code" / "main.py"), "type": "code"},
                ],
            }
        },
    )

    try:
        result = run_single_stage(task, "core_impl")

        assert result["status"] == "ok"
        assert result["stage"] == "core_impl"
        assert result["artifacts"] == [{"uri": str(workspace / "code" / "main.py"), "type": "code"}]
        rerun_done = next(evt for evt in reversed(app_module.state.history) if evt.task_id == task.task_id and evt.event_type == "StageRerunDone")
        assert rerun_done.payload["artifact_count"] == 1
    finally:
        app_module.state.tasks = original_tasks
        app_module.state.task_status = original_status
        app_module.state.history = original_history
