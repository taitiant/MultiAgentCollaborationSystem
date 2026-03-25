from pathlib import Path

import pytest

from core import Task, SystemState
from domains.software_dev.agents.patch_agent import PatchAgent
from storage.file_store import FileStore


def build_agent() -> PatchAgent:
    return PatchAgent(model_adapter=None)


def test_sanitize_strips_plain_path_header_for_python_file():
    agent = build_agent()
    raw = "code/main.py\nimport sys\nprint('ok')\n"

    sanitized = agent._sanitize_generated_content("code/main.py", raw)

    assert sanitized == "import sys\nprint('ok')\n"


def test_sanitize_extracts_target_block_from_multi_file_output():
    agent = build_agent()
    raw = (
        "code/main.py ->\n"
        "import sys\n"
        "print('main')\n\n"
        "code/game/constants.py ->\n"
        "FPS = 60\n"
    )

    sanitized = agent._sanitize_generated_content("code/main.py", raw)

    assert sanitized == "import sys\nprint('main')\n"


def test_sanitize_extracts_inline_target_block_from_multi_file_output():
    agent = build_agent()
    raw = (
        "requirements.txt -> pygame>=2.5.0\n\n"
        "code/main.py -> import sys\n"
        "print('main')\n\n"
        "code/game/constants.py -> FPS = 60\n"
    )

    sanitized = agent._sanitize_generated_content("code/main.py", raw)

    assert sanitized == "import sys\nprint('main')\n"


def test_sanitize_extracts_target_block_from_markdown_sections():
    agent = build_agent()
    raw = (
        "### code/main.py\n"
        "```python\n"
        "import sys\n"
        "print('main')\n"
        "```\n\n"
        "### code/game/constants.py\n"
        "```python\n"
        "FPS = 60\n"
        "```\n"
    )

    sanitized = agent._sanitize_generated_content("code/main.py", raw)

    assert sanitized == "import sys\nprint('main')\n"


def test_sanitize_strips_path_header_for_test_file():
    agent = build_agent()
    raw = "tests/test_board.py\nimport pytest\n\nfrom code.game.board import Board\n"

    sanitized = agent._sanitize_generated_content("tests/test_board.py", raw)

    assert sanitized == "import pytest\n\nfrom code.game.board import Board\n"


def test_sanitize_does_not_treat_python_dict_keys_as_path_headers():
    agent = build_agent()
    raw = (
        "PIECE_SHAPES = {\n"
        "    TetrominoType.I: {\n"
        "        0: ((0, 1), (1, 1), (2, 1), (3, 1)),\n"
        "    },\n"
        "}\n"
    )

    sanitized = agent._sanitize_generated_content("code/game/tetromino.py", raw)

    assert sanitized == raw
    agent._validate_generated_content("code/game/tetromino.py", sanitized)


def test_sanitize_normalizes_mojibake_direction_text_to_ascii():
    agent = build_agent()
    raw = (
        "controls = [\n"
        '    "Controls:",\n'
        '    "â\x86\x90 / â\x86\x92  Move",\n'
        '    "â\x86\x91 / Space Rotate",\n'
        '    "â\x86\x93  Soft Drop",\n'
        "]\n"
    )

    sanitized = agent._sanitize_generated_content("code/render.py", raw)

    assert "â\x86\x90" not in sanitized
    assert '"Left / Right  Move"' in sanitized
    assert '"Up / Space Rotate"' in sanitized
    assert '"Down  Soft Drop"' in sanitized


def test_derive_python_package_inits_adds_missing_package_roots():
    agent = build_agent()

    package_inits = agent._derive_python_package_inits([
        "code/game/board.py",
        "code/game/tetromino.py",
        "tests/test_board.py",
    ])

    assert "code/__init__.py" in package_inits
    assert "code/game/__init__.py" in package_inits
    assert "tests/__init__.py" in package_inits


def test_force_minimal_init_enabled_by_default():
    agent = build_agent()

    assert agent._should_force_minimal_init("code/game/__init__.py") is True
    assert agent._should_force_minimal_init("tests/__init__.py") is True
    assert agent._should_force_minimal_init("code/main.py") is False


def test_force_minimal_init_can_be_disabled():
    agent = build_agent()

    assert agent._should_force_minimal_init(
        "code/game/__init__.py",
        {"force_minimal_init_files": False},
    ) is False


def test_validate_rejects_code_package_imports():
    agent = build_agent()

    try:
        agent._validate_generated_content(
            "tests/test_board.py",
            "from code.game.board import Board\n",
        )
    except ValueError as exc:
        assert str(exc) == "generated_python_uses_code_package_import:tests/test_board.py"
    else:
        raise AssertionError("expected validation failure for code package imports")


def test_normalize_prompt_template_removes_multi_file_instructions():
    agent = build_agent()
    template = (
        "你是编码工程师。\n"
        "请按“逐文件输出”的方式生成代码：\n"
        "### path/to/file\n"
        "```<language>\n"
        "<content>\n"
        "```\n"
        "保留这一行。\n"
    )

    normalized = agent._normalize_prompt_template(template)

    assert "逐文件输出" not in normalized
    assert "### path/to/file" not in normalized
    assert "```<language>" not in normalized
    assert "<content>" not in normalized
    assert "保留这一行。" in normalized


def test_build_file_prompt_includes_allowed_runtime_modules():
    agent = build_agent()
    arch_text = "## 文件清单\ncode/main.py\ncode/game/__init__.py\ncode/game/game.py\n"

    prompt = agent._build_file_prompt("demo", "code/main.py", arch_text, None, Task(
        task_id="task-prompt",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={},
    ))

    assert "code/main.py" in prompt
    assert "game, game.game, main" in prompt or "main, game, game.game" in prompt
    assert "不要在代码中引用它（例如不存在的 app.*）" in prompt


def test_build_file_prompt_requires_thin_main_for_multi_module_python_project():
    agent = build_agent()
    arch_text = "## 文件清单\ncode/main.py\ncode/game_state.py\ncode/renderer.py\n"

    prompt = agent._build_file_prompt("demo", "code/main.py", arch_text, None, Task(
        task_id="task-main-entry",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={},
    ))

    assert "`code/main.py` 只负责程序入口、组装和主循环" in prompt
    assert "严禁出现 `from .main import ...`、`import main` 这类自导入" in prompt


def test_build_file_prompt_requires_ascii_ui_text():
    agent = build_agent()
    prompt = agent._build_file_prompt("demo", "code/render.py", "## 文件清单\ncode/render.py\n", None, Task(
        task_id="task-ascii-prompt",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={},
    ))

    assert "UI 字符串优先只使用 ASCII" in prompt
    assert "Left/Right/Up/Down" in prompt


def test_extract_architecture_files_moves_web_files_under_code_dir():
    agent = build_agent()
    arch_text = "## 文件清单\nindex.html\nstyle.css\nscript.js\n"

    files = agent._extract_architecture_files(arch_text)

    assert files == ["code/index.html", "code/style.css", "code/script.js"]


def test_extract_feedback_target_files_expands_brace_paths_and_single_files():
    agent = build_agent()
    feedback = (
        "当前同时存在两套并行的核心域实现（code/game/{constants,tetromino,board}.py 与 code/game/game.py）。"
        "另外 code/main.py 依赖缺失模块。"
    )
    known_files = [
        "code/main.py",
        "code/game/constants.py",
        "code/game/tetromino.py",
        "code/game/board.py",
        "code/game/game.py",
        "code/game/renderer.py",
    ]

    targets = agent._extract_feedback_target_files(feedback, known_files)

    assert targets == [
        "code/main.py",
        "code/game/constants.py",
        "code/game/tetromino.py",
        "code/game/board.py",
        "code/game/game.py",
    ]


def test_extract_feedback_target_files_understands_python_module_names():
    agent = build_agent()
    feedback = (
        "pytest 收集失败：ImportError while importing tests.test_board; "
        "game.tetromino 试图从 game.constants 导入不存在常量。"
    )
    known_files = [
        "code/main.py",
        "code/game/constants.py",
        "code/game/tetromino.py",
        "code/game/board.py",
        "code/game/game.py",
        "tests/test_board.py",
    ]

    targets = agent._extract_feedback_target_files(feedback, known_files)

    assert targets == [
        "code/game/constants.py",
        "code/game/tetromino.py",
        "tests/test_board.py",
    ]


def test_select_generation_files_prefers_selection_context_for_targeted_rework(tmp_path):
    agent = build_agent()
    workspace_root = tmp_path / "task-targeted-rework"
    for rel_path in [
        "code/main.py",
        "code/game/constants.py",
        "code/game/tetromino.py",
        "code/game/board.py",
        "code/game/game.py",
        "code/game/renderer.py",
        "tests/test_board.py",
        "docs/README.md",
    ]:
        abs_path = workspace_root / Path(rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text("# existing\n", encoding="utf-8")

    task = Task(
        task_id="task-targeted-rework",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "_runtime_collaboration": {
                "stage_name": "coding",
                "prompt_context": "[全局黑板]\n- 当前返工要求: 这是被截断的黑板上下文，没有任何可识别标记。",
                "selection_context": (
                    "[局部会话]\n"
                    "12. [review_feedback] 阶段评审: 请只修复 code/game/constants.py 与 code/game/tetromino.py 的导入错误。\n"
                    "13. [rework_request] 阶段评审: 继续返工，最小增量修复。\n"
                ),
            }
        },
        workspace_path=str(workspace_root),
    )

    files = [
        "code/main.py",
        "code/game/constants.py",
        "code/game/tetromino.py",
        "code/game/board.py",
        "code/game/game.py",
        "code/game/renderer.py",
        "tests/test_board.py",
        "docs/README.md",
    ]

    selected, meta = agent._select_generation_files(
        files,
        task,
        str(workspace_root),
        {"targeted_rework_enabled": True},
    )

    assert selected == [
        "code/game/constants.py",
        "code/game/tetromino.py",
    ]
    assert meta["mode"] == "targeted_rework"


def test_select_generation_files_prefers_code_over_existing_tests_for_smoke_feedback(tmp_path):
    agent = build_agent()
    workspace_root = tmp_path / "task-smoke-contract"
    for rel_path in [
        "code/main.py",
        "tests/test_smoke.py",
    ]:
        abs_path = workspace_root / Path(rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text("# existing\n", encoding="utf-8")

    task = Task(
        task_id="task-smoke-contract",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "_runtime_collaboration": {
                "stage_name": "coding",
                "selection_context": (
                    "[局部会话]\n"
                    "21. [smoke_feedback] 编码冒烟测试: pytest -q tests/test_smoke.py failed with KeyError: 'id'. "
                    "请按 tests/test_smoke.py 契约修复 code/main.py。\n"
                ),
            }
        },
        workspace_path=str(workspace_root),
    )

    selected, meta = agent._select_generation_files(
        ["code/main.py", "tests/test_smoke.py"],
        task,
        str(workspace_root),
        {"targeted_rework_enabled": True},
    )

    assert selected == ["code/main.py"]
    assert meta["mode"] == "targeted_rework"
    assert meta["preserve_existing_tests"] is True


def test_select_generation_files_uses_targeted_rework_for_test_handoff_even_without_explicit_file_match(tmp_path):
    agent = build_agent()
    workspace_root = tmp_path / "task-test-handoff"
    for rel_path in [
        "code/main.py",
        "code/game.py",
    ]:
        abs_path = workspace_root / Path(rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text("# existing\n", encoding="utf-8")

    task = Task(
        task_id="task-test-handoff",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "_runtime_collaboration": {
                "stage_name": "coding",
                "selection_context": (
                    "[局部会话]\n"
                    "31. [test_feedback] 测试工程师: pytest failed with AssertionError in gameplay regression. "
                    "请先修复核心逻辑，再回到全面测试。\n"
                ),
            }
        },
        workspace_path=str(workspace_root),
    )

    selected, meta = agent._select_generation_files(
        ["code/main.py", "code/game.py"],
        task,
        str(workspace_root),
        {"targeted_rework_enabled": True},
    )

    assert selected == ["code/main.py", "code/game.py"]
    assert meta["mode"] == "targeted_rework"
    assert meta["preserve_existing_tests"] is True


class _FakeModelAdapter:
    model_name = "fake-model"
    config = {}

    def generate(self, prompt: str, context: dict) -> str:
        if "目标文件路径：code/main.py" in prompt:
            return "print('ok')\n"
        raise AssertionError(f"unexpected prompt: {prompt}")


class _FakeSmokeModelAdapter:
    model_name = "fake-model"
    config = {}

    def generate(self, prompt: str, context: dict) -> str:
        if "目标文件路径：code/main.py" in prompt:
            return "def add(a, b):\n    return a + b\n"
        if "目标文件路径：tests/test_main.py" in prompt:
            return "from main import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        raise AssertionError(f"unexpected prompt: {prompt}")


class _FakeSurgicalModelAdapter:
    model_name = "fake-model"
    config = {}

    def __init__(self):
        self.prompts = []

    def generate(self, prompt: str, context: dict) -> str:
        self.prompts.append(prompt)
        if "SEARCH/REPLACE" not in prompt:
            raise AssertionError("expected surgical edit prompt")
        return (
            "<<<<< SEARCH\n"
            "def add(a, b):\n"
            "    return a - b\n"
            "=====\n"
            "def add(a, b):\n"
            "    return a + b\n"
            ">>>>> REPLACE\n"
        )


class _FakeSurgicalFallbackModelAdapter:
    model_name = "fake-model"
    config = {}

    def __init__(self):
        self.prompts = []
        self.calls = 0

    def generate(self, prompt: str, context: dict) -> str:
        self.prompts.append(prompt)
        self.calls += 1
        if self.calls == 1:
            return (
                "<<<<< SEARCH\n"
                "def add(a, b):\n"
                "    return a * b\n"
                "=====\n"
                "def add(a, b):\n"
                "    return a + b\n"
                ">>>>> REPLACE\n"
            )
        return "def add(a, b):\n    return a + b\n"


class _FakeExecutor:
    def __init__(self, results):
        self.results = list(results)
        self.commands = []

    def run(self, command, cwd=None, env=None):
        self.commands.append(command)
        if not self.results:
            raise AssertionError(f"unexpected command: {command}")
        return self.results.pop(0)


def test_act_emits_per_file_progress(tmp_path):
    task_id = "task-progress"
    workspace_root = tmp_path / task_id
    design_dir = workspace_root / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "architecture.md").write_text("## 文件清单\ncode/main.py\n", encoding="utf-8")

    progress_events = []
    agent = PatchAgent(
        model_adapter=_FakeModelAdapter(),
        storage=FileStore(str(tmp_path)),
        stage_name="coding",
        stage_type="coding",
        progress_callback=progress_events.append,
    )
    task = Task(
        task_id=task_id,
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(workspace_root),
    )

    agent.act(task, SystemState())

    assert any(event.get("progress_kind") == "batch" and event.get("progress_total") == 2 for event in progress_events)
    assert any(
        event.get("progress_kind") == "file"
        and event.get("file_status") == "start"
        and event.get("current_file") == "code/main.py"
        for event in progress_events
    )
    assert any(
        event.get("progress_kind") == "file"
        and event.get("file_status") == "done"
        and event.get("current_file") == "code/main.py"
        for event in progress_events
    )
    assert any(
        event.get("progress_kind") == "model"
        and event.get("progress_state") == "start"
        and event.get("current_file") == "code/main.py"
        for event in progress_events
    )
    assert any(
        event.get("progress_kind") == "model"
        and event.get("progress_state") == "done"
        and event.get("current_file") == "code/main.py"
        for event in progress_events
    )


def test_act_supports_custom_stage_name_with_coding_stage_type(tmp_path):
    task_id = "task-custom-stage"
    workspace_root = tmp_path / task_id
    design_dir = workspace_root / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "architecture.md").write_text("## 文件清单\ncode/main.py\n", encoding="utf-8")

    progress_events = []
    agent = PatchAgent(
        model_adapter=_FakeModelAdapter(),
        storage=FileStore(str(tmp_path)),
        stage_name="core_loop_build",
        stage_type="coding",
        progress_callback=progress_events.append,
    )
    task = Task(
        task_id=task_id,
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "spec": "demo",
            "event_configs": {
                "coding": {"generation_retry_limit": 2},
                "core_loop_build": {"generation_retry_limit": 1, "stage_type": "coding"},
            },
        },
        workspace_path=str(workspace_root),
    )

    agent.act(task, SystemState())

    cfg = agent._stage_config(task)
    assert cfg["generation_retry_limit"] == 1
    assert any(event.get("progress_kind") == "batch" for event in progress_events)


def test_act_runs_pytest_smoke_when_tests_exist(tmp_path):
    task_id = "task-smoke-pytest"
    workspace_root = tmp_path / task_id
    design_dir = workspace_root / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "architecture.md").write_text(
        "## 文件清单\ncode/main.py\ntests/test_main.py\n",
        encoding="utf-8",
    )

    executor = _FakeExecutor([
        {"exit_code": 0, "stdout": "", "stderr": ""},
        {"exit_code": 1, "stdout": "F", "stderr": "boom"},
    ])
    progress_events = []
    agent = PatchAgent(
        model_adapter=_FakeSmokeModelAdapter(),
        storage=FileStore(str(tmp_path)),
        stage_name="coding",
        stage_type="coding",
        progress_callback=progress_events.append,
    )
    agent.executor = executor
    task = Task(
        task_id=task_id,
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={"spec": "demo", "event_configs": {}},
        workspace_path=str(workspace_root),
    )

    message = agent.act(task, SystemState())

    assert executor.commands[0].startswith("python -m py_compile ")
    assert executor.commands[1] == "pytest -q --maxfail=1 tests/test_main.py"
    smoke_results = [art for art in message.artifacts if art.get("type") == "smoke_test_result"]
    assert len(smoke_results) == 2
    assert smoke_results[-1]["content"]["exit_code"] == 1
    assert any(
        event.get("progress_kind") == "smoke"
        and event.get("message") == "开始冒烟校验：pytest -q --maxfail=1 tests/test_main.py"
        for event in progress_events
    )


def test_apply_surgical_edits_replaces_only_target_snippet():
    agent = build_agent()
    original = (
        "def add(a, b):\n"
        "    return a - b\n\n"
        "def untouched():\n"
        "    return 'ok'\n"
    )
    patch = (
        "<<<<< SEARCH\n"
        "def add(a, b):\n"
        "    return a - b\n"
        "=====\n"
        "def add(a, b):\n"
        "    return a + b\n"
        ">>>>> REPLACE\n"
    )

    updated = agent._apply_surgical_edits("code/main.py", original, patch)

    assert "return a + b" in updated
    assert "return 'ok'" in updated
    assert "return a - b" not in updated


def test_validate_generated_content_rejects_python_self_import():
    agent = build_agent()

    with pytest.raises(ValueError, match="generated_python_self_import:code/main.py"):
        agent._validate_generated_content(
            "code/main.py",
            "from .main import main\n\n"
            "def main():\n"
            "    return 1\n",
        )


def test_generate_file_content_prefers_surgical_edits_for_existing_targeted_rework(tmp_path):
    task_id = "task-surgical-rework"
    workspace_root = tmp_path / task_id
    design_dir = workspace_root / "design"
    code_dir = workspace_root / "code"
    design_dir.mkdir(parents=True)
    code_dir.mkdir(parents=True)
    (design_dir / "architecture.md").write_text("## 文件清单\ncode/main.py\ncode/other.py\n", encoding="utf-8")
    (code_dir / "main.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n\n"
        "def untouched():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )

    model = _FakeSurgicalModelAdapter()
    agent = PatchAgent(
        model_adapter=model,
        storage=FileStore(str(tmp_path)),
        stage_name="coding",
        stage_type="coding",
    )
    task = Task(
        task_id=task_id,
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "spec": "demo",
            "event_configs": {},
            "_runtime_collaboration": {
                "stage_name": "coding",
                "selection_context": (
                    "[局部会话]\n"
                    "21. [smoke_feedback] 编码冒烟测试: tests/test_smoke.py failed, please fix code/main.py only.\n"
                ),
            },
        },
        workspace_path=str(workspace_root),
    )

    content, fallback_used = agent._generate_file_content(
        "demo",
        "code/main.py",
        "## 文件清单\ncode/main.py\ncode/other.py\n",
        None,
        task,
        generation_mode="targeted_rework",
    )

    assert fallback_used is False
    assert "return a + b" in content
    assert "return 'ok'" in content
    assert any("SEARCH/REPLACE" in prompt for prompt in model.prompts)


def test_generate_file_content_falls_back_to_full_regeneration_after_search_miss(tmp_path):
    task_id = "task-surgical-fallback"
    workspace_root = tmp_path / task_id
    design_dir = workspace_root / "design"
    code_dir = workspace_root / "code"
    design_dir.mkdir(parents=True)
    code_dir.mkdir(parents=True)
    (design_dir / "architecture.md").write_text("## 文件清单\ncode/main.py\n", encoding="utf-8")
    (code_dir / "main.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n",
        encoding="utf-8",
    )

    model = _FakeSurgicalFallbackModelAdapter()
    agent = PatchAgent(
        model_adapter=model,
        storage=FileStore(str(tmp_path)),
        stage_name="coding",
        stage_type="coding",
    )
    task = Task(
        task_id=task_id,
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "spec": "demo",
            "event_configs": {},
            "_runtime_collaboration": {
                "stage_name": "coding",
                "selection_context": (
                    "[局部会话]\n"
                    "41. [rework_request] 阶段评审: 继续返工，最小增量修复 code/main.py。\n"
                ),
            },
        },
        workspace_path=str(workspace_root),
    )

    content, fallback_used = agent._generate_file_content(
        "demo",
        "code/main.py",
        "## 文件清单\ncode/main.py\n",
        None,
        task,
        generation_mode="targeted_rework",
    )

    assert fallback_used is True
    assert content == "def add(a, b):\n    return a + b\n"
    assert "SEARCH/REPLACE" in model.prompts[0]
    assert "目标文件路径：code/main.py" in model.prompts[1]
    assert "输出一个或多个精确的 SEARCH/REPLACE 补丁块" not in model.prompts[1]
