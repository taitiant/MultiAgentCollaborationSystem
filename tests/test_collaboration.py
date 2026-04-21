from core import Task
import orchestration.collab as collaboration


def build_task() -> Task:
    return Task(
        task_id="task-1",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={},
    )


def test_stage_conversation_id_is_stable_and_scoped():
    conv_id = collaboration.stage_conversation_id("task-1", "implementation", "testing_handoff", peer_stage="verification")

    assert conv_id == "task-1::implementation::testing_handoff::verification"


def test_append_prompt_with_runtime_context_only_applies_to_matching_stage():
    task = build_task()
    task.context["_runtime_collaboration"] = {
        "stage_name": "implementation",
        "prompt_context": "[局部会话]\n1. reviewer: 请修复导入错误",
    }

    same_stage = collaboration.append_prompt_with_runtime_context("原始提示", task, "implementation")
    other_stage = collaboration.append_prompt_with_runtime_context("原始提示", task, "verification")

    assert "[阶段协作上下文]" in same_stage
    assert "请修复导入错误" in same_stage
    assert other_stage == "原始提示"


def test_build_stage_prompt_context_merges_blackboard_and_local_messages(monkeypatch):
    task = build_task()
    hub = collaboration.CollaborationHub(task)

    monkeypatch.setattr(
        collaboration.db,
        "list_blackboard_entries",
        lambda task_id, limit=8, stage_name=None: [
            {"title": "架构决策", "entry_key": "arch", "content": "统一使用 code/game 结构。"},
            {"title": "测试风险", "entry_key": "risk", "content": "pygame 依赖可能缺失。"},
        ],
    )
    monkeypatch.setattr(
        collaboration.db,
        "list_conversation_messages",
        lambda task_id, stage_name=None, conversation_id=None, limit=8: [
            {"turn_index": 1, "message_type": "review_feedback", "actor_role": "阶段评审", "actor_id": "reviewer", "content": "main.py 不应依赖 app.game_loop。"},
            {"turn_index": 2, "message_type": "smoke_feedback", "actor_role": "编码冒烟测试", "actor_id": "smoke", "content": "请修复 pytest 收集失败。"},
        ],
    )

    prompt_context = hub.build_stage_prompt_context("implementation")

    assert "[全局黑板]" in prompt_context
    assert "统一使用 code/game 结构" in prompt_context
    assert "[局部会话]" in prompt_context
    assert "main.py 不应依赖 app.game_loop" in prompt_context
    assert "请修复 pytest 收集失败" in prompt_context


def test_build_stage_prompt_context_filters_to_actionable_items(monkeypatch):
    task = build_task()
    hub = collaboration.CollaborationHub(task)

    monkeypatch.setattr(
        collaboration.db,
        "list_blackboard_entries",
        lambda task_id, limit=8, stage_name=None: [
            {"title": "当前返工要求", "entry_key": "rework", "entry_type": "rework_request", "content": "统一复用 game/constants.py。"},
            {"title": "最新交付", "entry_key": "delivery", "entry_type": "stage_delivery", "content": "这里不该进入提示。"},
        ],
    )
    monkeypatch.setattr(
        collaboration.db,
        "list_conversation_messages",
        lambda task_id, stage_name=None, conversation_id=None, limit=8: [
            {"turn_index": 1, "message_type": "submission", "actor_role": "PatchAgent", "actor_id": "patcher", "content": "我提交了一版实现。"},
            {"turn_index": 2, "message_type": "review_feedback", "actor_role": "阶段评审", "actor_id": "reviewer", "content": "不要重复定义规则表。"},
        ],
    )

    prompt_context = hub.build_stage_prompt_context("implementation")

    assert "统一复用 game/constants.py" in prompt_context
    assert "不要重复定义规则表" in prompt_context
    assert "这里不该进入提示" not in prompt_context
    assert "我提交了一版实现" not in prompt_context


def test_build_stage_prompt_context_includes_test_contract_snippet(monkeypatch, tmp_path):
    task = Task(
        task_id="task-contract-context",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={},
        workspace_path=str(tmp_path / "workspace"),
    )
    smoke_test = tmp_path / "workspace" / "tests" / "test_smoke.py"
    smoke_test.parent.mkdir(parents=True, exist_ok=True)
    smoke_test.write_text(
        "def test_initial_state_shape():\n"
        "    state = _make_state(width=8, height=6)\n"
        "    assert state['board'] == {'width': 8, 'height': 6}\n",
        encoding="utf-8",
    )
    hub = collaboration.CollaborationHub(task)

    monkeypatch.setattr(collaboration.db, "list_blackboard_entries", lambda task_id, limit=8, stage_name=None: [])
    monkeypatch.setattr(
        collaboration.db,
        "list_conversation_messages",
        lambda task_id, stage_name=None, conversation_id=None, limit=8: [
            {"turn_index": 1, "message_type": "smoke_feedback", "actor_role": "编码冒烟测试", "actor_id": "smoke", "content": "请按 tests/test_smoke.py 的契约修复 state 结构。"},
        ],
    )

    prompt_context = hub.build_stage_prompt_context("implementation")

    assert "[测试契约]" in prompt_context
    assert "tests/test_smoke.py" in prompt_context
    assert "assert state['board']" in prompt_context


def test_build_stage_prompt_context_includes_prerequisite_stage_artifacts(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    analysis_dir = workspace / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    req_path = analysis_dir / "requirements.md"
    req_path.write_text("运行环境：Python + Pygame。\n", encoding="utf-8")
    task = Task(
        task_id="task-prereq-context",
        domain="software",
        required_capabilities=["code.edit:v1"],
        context={
            "leader_plan": {
                "stages": [
                    {"name": "requirements_stage", "stage_type": "requirements", "label": "需求分析"},
                    {"name": "architecture_stage", "stage_type": "architecture", "label": "架构设计", "depends_on": ["requirements_stage"]},
                ]
            }
        },
        workspace_path=str(workspace),
    )
    hub = collaboration.CollaborationHub(task)

    monkeypatch.setattr(collaboration.db, "list_blackboard_entries", lambda *args, **kwargs: [])
    monkeypatch.setattr(collaboration.db, "list_conversation_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        collaboration.db,
        "get_latest_stage_done_event",
        lambda task_id, stage_name: {
            "payload": {"artifacts": [{"uri": str(req_path), "type": "md"}]}
        } if stage_name == "requirements_stage" else None,
    )

    prompt_context = hub.build_stage_prompt_context("architecture_stage")

    assert "[前置阶段产物：需求分析]" in prompt_context
    assert "requirements.md" in prompt_context
    assert "Python + Pygame" in prompt_context


def test_build_stage_prompt_context_separates_long_term_and_working_memory(monkeypatch):
    task = build_task()
    hub = collaboration.CollaborationHub(task)

    def fake_blackboard(task_id, limit=8, stage_name=None):
        entries = [
            {
                "title": "需求阶段决策",
                "entry_key": "stage:req:decision_memory",
                "entry_type": "decision_memory",
                "stage_name": "requirements_stage",
                "content": "需求分析已通过评审，可作为后续阶段默认依据。",
                "payload": {"pass": True},
            },
            {
                "title": "当前返工要求",
                "entry_key": "stage:impl:rework",
                "entry_type": "rework_request",
                "stage_name": "implementation",
                "content": "只修复 board.py 的边界处理。",
                "payload": {"pass": False},
            },
            {
                "title": "架构旧评审",
                "entry_key": "stage:arch:review",
                "entry_type": "stage_review",
                "stage_name": "architecture_stage",
                "content": "这条不该进入长期记忆。",
                "payload": {"pass": False},
            },
        ]
        if stage_name == "implementation":
            return [entries[1]]
        return entries

    monkeypatch.setattr(collaboration.db, "list_blackboard_entries", fake_blackboard)
    monkeypatch.setattr(
        collaboration.db,
        "list_conversation_messages",
        lambda task_id, stage_name=None, conversation_id=None, limit=8: [
            {
                "turn_index": 3,
                "message_type": "review_feedback",
                "actor_role": "阶段评审",
                "actor_id": "reviewer",
                "content": "继续最小增量修复边界判断。",
            }
        ],
    )

    prompt_context = hub.build_stage_prompt_context("implementation")

    assert "[长期决策记忆]" in prompt_context
    assert "需求分析已通过评审" in prompt_context
    assert "[短期工作记忆]" in prompt_context
    assert "只修复 board.py 的边界处理" in prompt_context
    assert prompt_context.count("需求分析已通过评审") == 1
    assert "这条不该进入长期记忆" not in prompt_context


def test_build_stage_targeted_context_prioritizes_stage_local_feedback(monkeypatch):
    task = build_task()
    hub = collaboration.CollaborationHub(task)

    def fake_blackboard(task_id, limit=8, stage_name=None):
        if stage_name == "implementation":
            return [
                {
                    "title": "当前返工要求",
                    "entry_key": "implementation-rework",
                    "entry_type": "rework_request",
                    "content": "只修复 code/game/{constants,tetromino}.py 的导入与接口。",
                },
                {
                    "title": "最新评审",
                    "entry_key": "implementation-review",
                    "entry_type": "stage_review",
                    "content": "game/game.py 需要对齐 Board.try_rotate 接口。",
                },
            ]
        return [
            {
                "title": "架构评审",
                "entry_key": "architecture-review",
                "entry_type": "stage_review",
                "content": "这是其它阶段的全局信息，不应该进入返工选文件上下文。",
            }
        ]

    monkeypatch.setattr(collaboration.db, "list_blackboard_entries", fake_blackboard)
    monkeypatch.setattr(
        collaboration.db,
        "list_conversation_messages",
        lambda task_id, stage_name=None, conversation_id=None, limit=8: [
            {
                "turn_index": 12,
                "message_type": "review_feedback",
                "actor_role": "阶段评审",
                "actor_id": "reviewer",
                "content": "请只返工 code/game/constants.py 与 code/game/tetromino.py。",
            },
            {
                "turn_index": 13,
                "message_type": "rework_request",
                "actor_role": "阶段评审",
                "actor_id": "reviewer",
                "content": "继续返工，最小增量修复导入错误。",
            },
        ],
    )

    targeted_context = hub.build_stage_targeted_context("implementation")

    assert "[局部会话]" in targeted_context
    assert "[review_feedback]" in targeted_context
    assert "[rework_request]" in targeted_context
    assert "[阶段黑板]" in targeted_context
    assert "[stage_review]" in targeted_context
    assert "[rework_request]" in targeted_context
    assert "这是其它阶段的全局信息" not in targeted_context


def test_summarize_decision_memory_prefers_settled_review_signal():
    summary = collaboration.CollaborationHub.summarize_decision_memory(
        "架构设计",
        {
            "output_summary": {
                "result_type": "md",
                "filename": "design/architecture.md",
            }
        },
        {
            "pass": True,
            "feedback": "目录结构与模块职责已经稳定。",
            "next_actions": ["编码阶段按该目录结构增量实现"],
        },
    )

    assert "架构设计 已通过评审" in summary
    assert "design/architecture.md" in summary
    assert "目录结构与模块职责已经稳定" in summary
    assert "编码阶段按该目录结构增量实现" in summary


def test_build_stage_review_context_keeps_external_feedback_only(monkeypatch):
    task = build_task()
    hub = collaboration.CollaborationHub(task)

    def fake_blackboard(task_id, limit=8, stage_name=None):
        return [
            {
                "title": "当前返工要求",
                "entry_key": "rework",
                "entry_type": "rework_request",
                "content": "这是阶段内返工噪音，不该进入 review 上下文。",
            },
            {
                "title": "测试反馈",
                "entry_key": "test-feedback",
                "entry_type": "test_feedback",
                "content": "全面测试指出锁定逻辑仍有缺陷。",
            },
        ]

    monkeypatch.setattr(collaboration.db, "list_blackboard_entries", fake_blackboard)
    monkeypatch.setattr(
        collaboration.db,
        "list_conversation_messages",
        lambda task_id, stage_name=None, conversation_id=None, limit=8: [
            {"turn_index": 1, "message_type": "review_feedback", "actor_role": "阶段评审", "actor_id": "reviewer", "content": "旧评审内容，不该进入当前 review。"},
            {"turn_index": 2, "message_type": "test_feedback", "actor_role": "测试工程师", "actor_id": "tester", "content": "请修复 Board 锁定后计分不更新。"},
        ],
    )

    review_context = hub.build_stage_review_context("implementation")

    assert "全面测试指出锁定逻辑仍有缺陷" in review_context
    assert "请修复 Board 锁定后计分不更新" in review_context
    assert "旧评审内容" not in review_context
    assert "返工噪音" not in review_context


def test_build_blackboard_snapshot_prefers_open_items_before_final_conclusion():
    snapshot = collaboration.build_blackboard_snapshot([
        {
            "entry_id": "review-1",
            "entry_key": "stage:impl:review",
            "entry_type": "stage_review",
            "title": "最新评审",
            "content": "评审结论：通过。当前实现满足要求。",
            "payload": {"pass": True},
            "updated_at": 20,
        },
        {
            "entry_id": "rework-1",
            "entry_key": "stage:impl:rework",
            "entry_type": "rework_request",
            "title": "当前返工要求",
            "content": "请修复导入错误并补齐测试。",
            "payload": {},
            "updated_at": 30,
        },
    ])

    assert snapshot["status"] == "open"
    assert "请修复导入错误并补齐测试" in snapshot["shared_context"]
    assert snapshot["open_items"] == ["请修复导入错误并补齐测试。"] or "请修复导入错误并补齐测试" in snapshot["open_items"][0]
    assert snapshot["final_conclusion"] == ""


def test_render_blackboard_snapshot_text_prefers_final_conclusion_when_no_open_items():
    snapshot = collaboration.build_blackboard_snapshot([
        {
            "entry_id": "review-1",
            "entry_key": "stage:impl:review",
            "entry_type": "stage_review",
            "title": "最新评审",
            "content": "评审结论：通过。当前实现满足要求。",
            "payload": {"pass": True},
            "updated_at": 40,
        },
        {
            "entry_id": "delivery-1",
            "entry_key": "stage:impl:delivery",
            "entry_type": "stage_delivery",
            "title": "最新交付",
            "content": "已交付实现与测试产物。",
            "payload": {},
            "updated_at": 30,
        },
    ])

    text = collaboration.render_blackboard_snapshot_text(snapshot)

    assert "[当前共享结论]" in text
    assert "[最终结论]" in text
    assert "评审结论：通过" in text
