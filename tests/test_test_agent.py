from core import Task, SystemState
from domains.software_dev.agents.test_agent import TestAgent as RuntimeTestAgent


class _FakeExecutor:
    def __init__(self, results):
        self.results = list(results)
        self.commands = []

    def run(self, command, cwd=None, env=None):
        self.commands.append(command)
        if not self.results:
            raise AssertionError(f"unexpected command: {command}")
        return self.results.pop(0)


def test_act_emits_testing_progress_for_pytest(tmp_path):
    task_id = "task-testing-progress"
    workspace = tmp_path / task_id
    tests_dir = workspace / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    progress_events = []
    agent = RuntimeTestAgent(
        executor=_FakeExecutor([{"exit_code": 0, "stdout": "ok", "stderr": ""}]),
        workspace_root=str(tmp_path),
        stage_name="testing",
        stage_type="testing",
        progress_callback=progress_events.append,
    )
    task = Task(
        task_id=task_id,
        domain="software",
        required_capabilities=["test.run:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(workspace),
    )

    agent.act(task, SystemState())

    assert any(
        event.get("progress_kind") == "test"
        and event.get("progress_state") == "start"
        for event in progress_events
    )
    assert any(
        event.get("progress_kind") == "test"
        and event.get("progress_state") == "done"
        for event in progress_events
    )


def test_act_emits_compile_and_report_progress_when_no_tests(tmp_path):
    task_id = "task-compile-progress"
    workspace = tmp_path / task_id
    code_dir = workspace / "code"
    code_dir.mkdir(parents=True)
    (code_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")

    progress_events = []
    agent = RuntimeTestAgent(
        executor=_FakeExecutor([
            {"exit_code": 0, "stdout": "", "stderr": ""},
            {"exit_code": 0, "stdout": "", "stderr": ""},
        ]),
        workspace_root=str(tmp_path),
        stage_name="testing",
        stage_type="testing",
        progress_callback=progress_events.append,
    )
    task = Task(
        task_id=task_id,
        domain="software",
        required_capabilities=["test.run:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(workspace),
    )

    agent.act(task, SystemState())

    assert any(
        event.get("progress_kind") == "compile"
        and event.get("progress_state") == "start"
        for event in progress_events
    )
    assert any(
        event.get("progress_kind") == "compile"
        and event.get("progress_state") == "done"
        for event in progress_events
    )
    assert any(
        event.get("progress_kind") == "report"
        and event.get("progress_state") == "done"
        for event in progress_events
    )


def test_act_generates_web_manual_report_without_python_or_tetris_fallback(tmp_path):
    task_id = "task-web-report"
    workspace = tmp_path / task_id
    workspace.mkdir(parents=True)
    (workspace / "index.html").write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="style.css"></head>'
        '<body><script src="script.js"></script></body></html>\n',
        encoding="utf-8",
    )
    (workspace / "style.css").write_text("body { background: #000; }\n", encoding="utf-8")
    (workspace / "script.js").write_text("console.log('flappy');\n", encoding="utf-8")

    agent = RuntimeTestAgent(
        executor=_FakeExecutor([]),
        workspace_root=str(tmp_path),
        stage_name="testing",
        stage_type="testing",
        progress_callback=None,
    )
    task = Task(
        task_id=task_id,
        domain="software",
        required_capabilities=["test.run:v1"],
        context={"spec": "编写一个flappy bird小游戏", "event_configs": {}},
        workspace_path=str(workspace),
    )

    result = agent.act(task, SystemState())

    artifacts = result.artifacts or []
    test_result = next(artifact for artifact in artifacts if artifact.get("type") == "test_result")
    report_artifact = next(artifact for artifact in artifacts if artifact.get("type") == "md")

    assert test_result["content"]["command"] == "web-static-check"
    assert int(test_result["content"]["exit_code"]) == 0

    report_text = (tmp_path / task_id / "tests" / "manual_test_report.md").read_text(encoding="utf-8")
    assert "Flappy Bird" not in report_text or "任务需求：编写一个flappy bird小游戏" in report_text
    assert "方块自动下落" not in report_text
    assert "旋转" not in report_text
    assert "未发现 Python 源文件" not in report_text
    assert "静态资源校验" in report_text
    assert "`index.html`" in report_text
    assert report_artifact["uri"].endswith("tests/manual_test_report.md")


def test_act_adds_startup_smoke_result_for_python_projects_without_tests(tmp_path):
    task_id = "task-python-startup-smoke"
    workspace = tmp_path / task_id
    code_dir = workspace / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "__init__.py").write_text("", encoding="utf-8")
    (code_dir / "main.py").write_text("from . import __init__\n", encoding="utf-8")

    agent = RuntimeTestAgent(
        executor=_FakeExecutor([
            {"exit_code": 0, "stdout": "", "stderr": ""},
            {"exit_code": 0, "stdout": "", "stderr": ""},
        ]),
        workspace_root=str(tmp_path),
        stage_name="testing",
        stage_type="testing",
        progress_callback=None,
    )
    task = Task(
        task_id=task_id,
        domain="software",
        required_capabilities=["test.run:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(workspace),
    )

    result = agent.act(task, SystemState())

    artifacts = result.artifacts or []
    startup = next(artifact for artifact in artifacts if artifact.get("type") == "startup_smoke_result")
    assert "importlib.import_module" in startup["content"]["command"]
    report_text = (tmp_path / task_id / "tests" / "manual_test_report.md").read_text(encoding="utf-8")
    assert "入口冒烟校验" in report_text
    assert "退出码：0" in report_text


def test_manual_report_marks_compile_failure_as_failure_text(tmp_path):
    task = Task(
        task_id="task-report-failure",
        domain="software",
        required_capabilities=[],
        context={"spec": "demo"},
        workspace_path=str(tmp_path / "workspace"),
    )
    agent = RuntimeTestAgent(workspace_root=str(tmp_path))

    report = agent._manual_report(
        task,
        str(tmp_path),
        project_stack="python",
        source_files=[],
        compile_result={"command": "python -m py_compile x.py", "exit_code": 1},
        startup_result={"command": "python -c \"import x\"", "exit_code": 1},
    )

    assert "编译校验失败" in report
    assert "入口导入/启动链校验失败" in report
