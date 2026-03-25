from __future__ import annotations
import os
import yaml
import db
import json
import re
import shutil
from pathlib import PurePosixPath
from typing import Dict, Any, List, Callable, Optional
from langgraph.graph import StateGraph
from langgraph.graph import END

from core import Task, SystemState, new_event
from adapters.model_registry import ModelRegistry
from domains.software_dev.agents.patch_agent import PatchAgent
from domains.software_dev.agents.test_agent import TestAgent
from orchestration.collaboration import CollaborationHub, append_prompt_with_runtime_context
from orchestration.file_utils import write_text, ensure_workspace
from orchestration.workspace_cleanup import cleanup_architecture_orphan_files
from core import AgentMessage
from storage.file_store import FileStore

DEFAULT_STAGE_PROMPTS = {
    "requirements": (
        "你是需求分析师。基于用户需求输出严格的需求文档。\n"
        "需求：{spec}\n"
        "输出 Markdown，必须包含：\n"
        "1) 功能需求\n2) 非功能需求\n3) 验收标准\n4) 边界与约束\n"
        "禁止输出架构设计、目录结构、代码文件规划。"
    ),
    "architecture": (
        "你是架构设计师。基于需求文档设计系统架构与文件结构。\n"
        "原始需求：{spec}\n"
        "输出 Markdown，必须包含：\n"
        "1) 架构分层说明\n2) 模块职责\n3) 接口/数据流\n"
        "4) 一个标题为“## 文件清单”的章节，按每行一个相对路径列出文件（可包含深层目录），例如 code/api/app.py。\n"
        "不要输出实现代码。"
    ),
    "coding": (
        "你是编码工程师。请基于需求与架构文档，为指定文件生成内容。\n"
        "需求：{spec}\n"
        "必须遵循架构文档中的文件路径和职责，不得擅自改动路径。"
    ),
    "testing": (
        "你是测试工程师。请基于需求、设计和现有实现执行验证。\n"
        "需求：{spec}\n"
        "优先覆盖主流程、关键边界和最近改动带来的回归风险。"
    ),
    "docs": (
        "你是文档工程师。请基于当前项目实现与测试结果生成 README。\n"
        "需求：{spec}\n"
        "输出应包含运行方式、配置、输入输出示例、限制说明。"
    ),
}

DEFAULT_ACCEPTANCE_CRITERIA = {
    "requirements": "需求范围、约束、验收标准明确，没有越界到架构或实现细节。",
    "architecture": "架构方案可支撑需求实现，并给出清晰可执行的文件清单。",
    "coding": "实现与需求和设计一致，生成代码可通过基础语法/冒烟校验。",
    "testing": "测试结果能覆盖关键功能与主要风险，并明确记录失败原因或回退结论。",
    "docs": "交付文档说明清晰，覆盖运行方式、配置、限制与验证结果。",
}

ARCHITECTURE_FILE_LIST_HINT = (
    "\n\n输出要求补充：必须包含标题为“## 文件清单”的章节，"
    "并按每行一个相对路径列出待实现文件（例如 code/main.py、code/game/board.py）。"
)

TEXT_OUTPUT_QUALITY_GUARDRAIL = (
    "\n\n输出前自检：\n"
    "- 通读全文一遍，修正明显错别字、漏字、残句和重复标点。\n"
    "- 不要出现“少一个字”“半句话断掉”“重复两个标点”这类低级文本问题。\n"
    "- 保持标题、列表、路径和术语前后一致。"
)

DEFAULT_BLOCKING_REVIEW_STAGE_TYPES = {"requirements", "architecture", "docs"}
ARCHITECTURE_FILE_SECTION_TOKENS = ("文件清单", "文件列表")
ARCHITECTURE_FILE_SECTION_COMPAT_TOKENS = (
    "文件清单",
    "文件列表",
    "文件单",
    "文件目录",
    "目录清单",
)
ARCHITECTURE_ROOT_FILES = {
    "index.html", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "tsconfig.json", "tsconfig.app.json", "vite.config.ts", "vite.config.js",
    "requirements.txt", "pyproject.toml", "setup.py", "README.md",
}
PRESERVED_WORKSPACE_PREFIXES = ("analysis/", "design/", "docs/", "plan/", "logs/", "tests/")
WEB_ROOT_MANAGED_PREFIXES = ("src/", "public/", "assets/", "scripts/", "styles/")
WEB_ROOT_MANAGED_FILES = {
    "index.html",
    "style.css",
    "script.js",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig.json",
    "tsconfig.app.json",
    "vite.config.ts",
    "vite.config.js",
}
WEB_STACK_EXTENSIONS = {"html", "css", "scss", "js", "jsx", "ts", "tsx"}
PYTHON_STACK_EXTENSIONS = {"py"}
NEUTRAL_STACK_EXTENSIONS = {"md", "txt", "json", "yaml", "yml", "toml", "ini", "sh"}
ARCHITECTURE_PATH_PATTERN = re.compile(
    r"(?P<path>(?:\.?/)?(?:[\w\-]+/)*[\w\-.]+\.[A-Za-z0-9]+|(?:\.?/)?(?:[\w\-]+/)+)"
)

STAGE_TYPE_ALIASES = {
    "requirements": "requirements",
    "requirement": "requirements",
    "analysis": "requirements",
    "analyst": "requirements",
    "clarification": "requirements",
    "clarify": "requirements",
    "scope": "requirements",
    "product": "requirements",
    "planning": "requirements",
    "需求": "requirements",
    "需求分析": "requirements",
    "需求澄清": "requirements",
    "architecture": "architecture",
    "arch": "architecture",
    "design": "architecture",
    "solution": "architecture",
    "solution_design": "architecture",
    "technical_design": "architecture",
    "架构": "architecture",
    "设计": "architecture",
    "方案": "architecture",
    "coding": "coding",
    "code": "coding",
    "implementation": "coding",
    "implement": "coding",
    "develop": "coding",
    "development": "coding",
    "build": "coding",
    "patch": "coding",
    "fix": "coding",
    "bugfix": "coding",
    "repair": "coding",
    "编码": "coding",
    "开发": "coding",
    "实现": "coding",
    "修复": "coding",
    "testing": "testing",
    "test": "testing",
    "qa": "testing",
    "verification": "testing",
    "verify": "testing",
    "validation": "testing",
    "review": "testing",
    "验收": "testing",
    "测试": "testing",
    "验证": "testing",
    "docs": "docs",
    "doc": "docs",
    "documentation": "docs",
    "readme": "docs",
    "handoff": "docs",
    "交付": "docs",
    "文档": "docs",
    "说明": "docs",
}

STAGE_EXECUTOR_TYPES = {"requirements", "architecture", "coding", "testing", "docs"}


def _normalize_conversation_group(value: Any) -> Dict[str, Any] | None:
    if not value:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        return {"key": raw, "label": raw}
    if not isinstance(value, dict):
        return None
    raw_key = value.get("key") or value.get("id") or value.get("name")
    if not raw_key:
        return None
    key = str(raw_key).strip()
    if not key:
        return None
    normalized = {
        "key": key,
        "label": str(value.get("label") or value.get("title") or key).strip() or key,
    }
    kind = str(value.get("kind") or value.get("type") or "").strip()
    if kind:
        normalized["kind"] = kind
    return normalized


def _emit_stage_progress(progress_callback: Optional[Callable[[Dict[str, Any]], None]], **payload: Any) -> None:
    if not progress_callback:
        return
    try:
        progress_callback(payload)
    except Exception:
        return


def _model_failure_text(value: Any) -> str:
    text = str(value or "")
    if not text.startswith("["):
        return ""
    lowered = text.lower()
    if "error" in lowered or "empty response" in lowered or "disabled" in lowered:
        return text
    return ""


def _build_rework_guidance(stage_type: str, feedback: str, attempt: int = 0) -> str:
    normalized_type = normalize_stage_type(stage_type)
    raw = str(feedback or "")
    compact = raw.lower()
    hints: List[str] = []
    if normalized_type == "coding":
        hints.append("只处理评审明确指出的问题，优先做最小必要修改，不要顺手扩写无关功能。")
        hints.append("必须与 architecture.md 的文件清单保持一致；若调用方引用了不存在的模块，优先修改调用方去对齐清单内已有文件，不要随意新增清单外路径。")
        hints.append("若多个文件出现重复定义，请保留单一事实来源，其余文件改为 import / 复用 / 委托，不要复制常量、规则表、状态模型。")
        if any(token in compact for token in ("重复", "并行", "两套", "一致性", "duplicate")):
            hints.append("本轮重点先消除结构重复与职责冲突，再考虑补充细节实现。")
        if any(token in compact for token in ("缺失", "不存在", "未", "导入", "import", "模块")):
            hints.append("本轮重点确保入口、导入路径与实际文件一一对应，可直接启动或至少可完成基础运行校验。")
        if any(token in compact for token in ("keyerror", "attributeerror", "typeerror", "assert", "unexpected keyword", "missing 1 required positional argument")):
            hints.append("若失败来自测试断言或调用异常，优先对齐公开接口契约：函数签名、返回结构、字段名必须与现有测试和调用方一致，不要只修局部逻辑。")
        if any(token in compact for token in ("pytest", "test_", "smoke", "接口", "验收")):
            hints.append("优先修复当前测试直接指出的问题；除非明确要求，不要通过修改测试来规避失败。")
    elif normalized_type == "testing":
        hints.append("优先修复会直接导致测试失败或无法验证的问题，保持测试报告可复现。")
    if attempt > 0:
        hints.append(f"这是第 {attempt + 1} 次评审返工，请先把上述阻塞项彻底收敛，再提交下一版。")
    return "\n".join(f"- {item}" for item in hints if item)


def _normalize_decision_options(value: Any, *, limit: int = 4) -> List[str]:
    if not isinstance(value, list):
        return []
    options: List[str] = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        options.append(text)
        if len(options) >= limit:
            break
    return options


def _extract_agent_decision_candidates(payload: Dict[str, Any]) -> Dict[str, Any] | None:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    candidate = metadata.get("human_decision") if isinstance(metadata.get("human_decision"), dict) else {}
    if not candidate:
        return None
    question = str(candidate.get("question") or "").strip()
    reason = str(candidate.get("reason") or candidate.get("why_blocked") or "").strip()
    options = _normalize_decision_options(candidate.get("options"))
    if not (question or reason or options):
        return None
    return {
        "question": question,
        "reason": reason,
        "options": options,
    }


def _extract_human_decision_request(
    stage_name: str,
    stage_type: str,
    stage_label: str,
    review: Dict[str, Any],
) -> Dict[str, Any] | None:
    if not isinstance(review, dict):
        return None
    required = review.get("human_decision_required")
    if required is None:
        required = review.get("requires_human_decision")
    if required is None and isinstance(review.get("human_decision"), dict):
        required = True
    if required is not True:
        return None
    request_payload = review.get("human_decision") if isinstance(review.get("human_decision"), dict) else review
    question = str(
        request_payload.get("decision_question")
        or request_payload.get("question")
        or review.get("feedback")
        or f"{stage_label} 需要人工决策"
    ).strip()
    reason = str(
        request_payload.get("decision_reason")
        or request_payload.get("why_blocked")
        or request_payload.get("reason")
        or review.get("feedback")
        or ""
    ).strip()
    options = _normalize_decision_options(
        request_payload.get("decision_options")
        or request_payload.get("options")
    )
    return {
        "kind": "human_decision",
        "stage": stage_name,
        "stage_type": stage_type,
        "label": stage_label,
        "question": question,
        "options": options,
        "why_blocked": reason,
        "requested_by": "leader_review",
    }


def _is_review_blocking(stage_type: str, stage_cfg: Dict[str, Any]) -> bool:
    raw = stage_cfg.get("review_blocking")
    if raw is None:
        return normalize_stage_type(stage_type) in DEFAULT_BLOCKING_REVIEW_STAGE_TYPES
    return bool(raw)


def _load_task_artifact_text(task: Task, rel_path: str) -> str:
    workspace = os.path.abspath(task.workspace_path or "")
    if not workspace:
        return ""
    abs_path = os.path.join(workspace, rel_path)
    if not os.path.exists(abs_path):
        return ""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _is_architecture_file_section_heading(line: str) -> bool:
    heading = str(line or "").strip()
    if not heading.startswith("## "):
        return False
    title = heading[3:].strip().replace("：", "").replace(":", "").replace(" ", "")
    if not title or "文件" not in title:
        return False
    if any(token in title for token in ARCHITECTURE_FILE_SECTION_TOKENS):
        return True
    if any(token in title for token in ARCHITECTURE_FILE_SECTION_COMPAT_TOKENS):
        return True
    return False


def _find_architecture_file_sections(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    lines = text.splitlines()
    sections: list[tuple[int, int]] = []
    start: int | None = None
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if _is_architecture_file_section_heading(line):
            if start is not None:
                sections.append((start, idx))
            start = idx
            continue
        if start is not None and line.startswith("## "):
            sections.append((start, idx))
            start = None
    if start is not None:
        sections.append((start, len(lines)))
    return lines, sections


def _extract_file_paths_from_lines(lines: list[str]) -> list[str]:
    candidates: list[str] = []
    seen = set()
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        match = ARCHITECTURE_PATH_PATTERN.search(line.replace("`", ""))
        if not match:
            continue
        path = match.group("path").strip().lstrip("./").replace("\\", "/")
        if not path or path.startswith("/") or path.startswith(".."):
            continue
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if "/" not in path and "." not in path and path not in ARCHITECTURE_ROOT_FILES:
            continue
        if ext and ext not in WEB_STACK_EXTENSIONS | PYTHON_STACK_EXTENSIONS | NEUTRAL_STACK_EXTENSIONS:
            continue
        if path not in seen:
            seen.add(path)
            candidates.append(path)
    return candidates


def _extract_architecture_file_list(text: str) -> list[str]:
    lines, sections = _find_architecture_file_sections(text)
    candidates: list[str] = []
    seen = set()
    for start, end in sections:
        for path in _extract_file_paths_from_lines(lines[start + 1:end]):
            if path not in seen:
                seen.add(path)
                candidates.append(path)
    return candidates


def _normalize_project_implementation_path(path: str, project_stack: str) -> str:
    normalized = str(path or "").strip().lstrip("./").replace("\\", "/")
    if not normalized:
        return ""
    if normalized.startswith(("code/", "app/")):
        return normalized
    if normalized.startswith(PRESERVED_WORKSPACE_PREFIXES):
        return normalized
    if project_stack != "web":
        return normalized
    if normalized.startswith(WEB_ROOT_MANAGED_PREFIXES):
        return f"code/{normalized}"
    name = PurePosixPath(normalized).name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if "/" not in normalized and (normalized in WEB_ROOT_MANAGED_FILES or ext in WEB_STACK_EXTENSIONS):
        return f"code/{normalized}"
    return normalized


def _normalize_architecture_file_list(paths: list[str], project_stack: str) -> list[str]:
    normalized: list[str] = []
    seen = set()
    for path in paths:
        candidate = _normalize_project_implementation_path(path, project_stack)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _classify_architecture_path(path: str) -> str:
    normalized = str(path or "").strip().lower().replace("\\", "/")
    ext = normalized.rsplit(".", 1)[-1] if "." in normalized else ""
    if (
        normalized in {"index.html", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json", "tsconfig.app.json", "vite.config.ts", "vite.config.js"}
        or normalized.startswith(("src/", "public/"))
        or ext in WEB_STACK_EXTENSIONS
    ):
        return "web"
    if (
        normalized in {"requirements.txt", "pyproject.toml", "setup.py"}
        or ext in PYTHON_STACK_EXTENSIONS
        or normalized.startswith(("code/", "app/"))
    ):
        return "python"
    return "neutral"


def _stack_keyword_scores(*texts: str) -> tuple[int, int]:
    merged = "\n".join(str(text or "") for text in texts).lower()
    web_score = 0
    python_score = 0
    for token in ("浏览器", "web", "html", "canvas", "vite", "typescript", "javascript", "esm", "package.json", "前端"):
        if token in merged:
            web_score += 1
    for token in ("python", "pygame", "requirements.txt", "pyproject", "pip", "命令行"):
        if token in merged:
            python_score += 1
    return web_score, python_score


def _infer_declared_stack(text: str, *, fallback: str | None = None) -> str | None:
    web_score, python_score = _stack_keyword_scores(text)
    if web_score == python_score:
        return fallback
    return "web" if web_score > python_score else "python"


def _infer_project_stack(spec: str, requirements_text: str, arch_text: str, candidates: list[str] | None = None) -> str:
    merged = "\n".join([str(spec or ""), str(requirements_text or ""), str(arch_text or "")]).lower()
    web_score, python_score = _stack_keyword_scores(spec, requirements_text, arch_text)
    for path in candidates or _extract_architecture_file_list(arch_text):
        stack = _classify_architecture_path(path)
        if stack == "web":
            web_score += 2
        elif stack == "python":
            python_score += 2
    if web_score > python_score:
        return "web"
    if python_score > web_score:
        return "python"
    return "web" if any(token in merged for token in ("俄罗斯方块", "tetris", "canvas", "浏览器")) else "python"


def _fallback_architecture_file_list(project_stack: str) -> list[str]:
    if project_stack == "web":
        return [
            "code/package.json",
            "code/index.html",
            "code/src/main.ts",
            "code/src/game/board.ts",
            "code/src/game/pieces.ts",
            "code/src/game/game.ts",
            "code/src/render/renderer.ts",
            "code/src/input/keyboard.ts",
            "code/src/styles.css",
            "docs/README.md",
        ]
    return [
        "requirements.txt",
        "code/main.py",
        "code/game/__init__.py",
        "code/game/constants.py",
        "code/game/tetromino.py",
        "code/game/board.py",
        "code/game/game.py",
        "code/game/renderer.py",
        "tests/test_board.py",
        "docs/README.md",
    ]


def _select_architecture_file_list(candidates: list[str], project_stack: str) -> list[str]:
    if not candidates:
        return _fallback_architecture_file_list(project_stack)
    selected = [path for path in candidates if _classify_architecture_path(path) in {project_stack, "neutral"}]
    if not any(_classify_architecture_path(path) == project_stack for path in selected):
        selected = [path for path in candidates if _classify_architecture_path(path) != ("python" if project_stack == "web" else "web")]
    selected = selected or candidates
    return _normalize_architecture_file_list(selected, project_stack)


def _strip_architecture_file_sections(text: str) -> str:
    lines, sections = _find_architecture_file_sections(text)
    if not sections:
        return text.rstrip()
    kept: list[str] = []
    cursor = 0
    for start, end in sections:
        kept.extend(lines[cursor:start])
        cursor = end
    kept.extend(lines[cursor:])
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept).rstrip()


def _normalize_architecture_markdown(spec: str, requirements_text: str, arch_text: str) -> str:
    candidates = _extract_architecture_file_list(arch_text)
    project_stack = _infer_declared_stack(requirements_text) or _infer_project_stack(spec, requirements_text, arch_text, candidates)
    selected = _select_architecture_file_list(candidates, project_stack)
    selected = list(dict.fromkeys(selected))
    base = _strip_architecture_file_sections(arch_text)
    section = "## 文件清单\n" + "\n".join(selected)
    if not base.strip():
        return section + "\n"
    return base.rstrip() + "\n\n" + section + "\n"


def _architecture_validation_issues(spec: str, requirements_text: str, arch_text: str) -> list[str]:
    lines, sections = _find_architecture_file_sections(arch_text)
    candidates = _extract_architecture_file_list(arch_text)
    issues: list[str] = []
    if len(sections) != 1:
        issues.append("架构文档应只保留一个“## 文件清单”章节。")
    if not candidates:
        issues.append("架构文档缺少可提取的文件路径清单。")
        return issues
    project_stack = _infer_project_stack(spec, requirements_text, arch_text, candidates)
    candidates = _normalize_architecture_file_list(candidates, project_stack)
    required_stack = _infer_declared_stack(requirements_text)
    web_paths = [path for path in candidates if _classify_architecture_path(path) == "web"]
    python_paths = [path for path in candidates if _classify_architecture_path(path) == "python"]
    if web_paths and python_paths:
        issues.append("文件清单同时混入了 Web/TS 与 Python 两套技术栈。")
    if required_stack and required_stack != project_stack:
        issues.append(f"架构方案未遵循上游需求已给定的默认技术栈：需求偏向 {required_stack}，当前架构却偏向 {project_stack}。")
    if project_stack == "web":
        if python_paths:
            issues.append("当前需求更接近 Web 交付，但文件清单仍包含 Python 项目路径。")
        if not any(path.lower().endswith(".html") for path in candidates):
            issues.append("Web 方案文件清单缺少 HTML 入口文件。")
    if project_stack == "python" and web_paths:
        issues.append("当前需求更接近 Python 交付，但文件清单仍包含前端工程路径。")
    return issues


def _docs_validation_issues(readme_text: str) -> list[str]:
    text = str(readme_text or "")
    normalized = text.lower()
    issues: list[str] = []
    if not normalized.strip():
        return ["README 内容为空。"]

    section_groups = {
        "运行方式": ("启动方式", "运行方式", "如何运行"),
        "文件结构": ("文件结构", "项目结构", "目录结构"),
        "限制说明": ("限制", "注意事项", "已知问题"),
        "测试结论": ("测试", "验证结论", "测试/验证", "验证结果"),
    }
    for label, tokens in section_groups.items():
        if not any(token.lower() in normalized for token in tokens):
            issues.append(f"README 缺少“{label}”相关章节或说明。")
    if "python" not in normalized and "pip install" not in normalized:
        issues.append("README 未清晰说明基础运行环境或依赖安装方式。")
    return issues


def _review_feedback_is_evidence_limited(feedback_text: str) -> bool:
    text = str(feedback_text or "")
    if not text:
        return False
    uncertain_markers = ("无法确认", "尚不能确认", "证据不足", "未见", "可能缺少", "大概率", "当前可见证据")
    return any(marker in text for marker in uncertain_markers)

STAGE_TYPE_BLUEPRINTS = {
    "requirements": {
        "name": "requirements",
        "stage_type": "requirements",
        "label": "需求分析",
        "role": "需求分析师",
        "capabilities": ["analysis.requirements:v1"],
        "prompt_template": DEFAULT_STAGE_PROMPTS["requirements"],
        "acceptance_criteria": DEFAULT_ACCEPTANCE_CRITERIA["requirements"],
        "human_checkpoint": False,
    },
    "architecture": {
        "name": "architecture",
        "stage_type": "architecture",
        "label": "架构设计",
        "role": "架构设计师",
        "capabilities": ["design.arch:v1"],
        "prompt_template": DEFAULT_STAGE_PROMPTS["architecture"],
        "acceptance_criteria": DEFAULT_ACCEPTANCE_CRITERIA["architecture"],
        "human_checkpoint": False,
    },
    "coding": {
        "name": "coding",
        "stage_type": "coding",
        "label": "编码实现",
        "role": "软件工程师",
        "capabilities": ["code.edit:v1"],
        "prompt_template": DEFAULT_STAGE_PROMPTS["coding"],
        "acceptance_criteria": DEFAULT_ACCEPTANCE_CRITERIA["coding"],
        "human_checkpoint": False,
    },
    "testing": {
        "name": "testing",
        "stage_type": "testing",
        "label": "测试验证",
        "role": "测试工程师",
        "capabilities": ["test.run:v1"],
        "prompt_template": DEFAULT_STAGE_PROMPTS["testing"],
        "acceptance_criteria": DEFAULT_ACCEPTANCE_CRITERIA["testing"],
        "human_checkpoint": False,
    },
    "docs": {
        "name": "docs",
        "stage_type": "docs",
        "label": "文档交付",
        "role": "文档工程师",
        "capabilities": ["doc.write:v1"],
        "prompt_template": DEFAULT_STAGE_PROMPTS["docs"],
        "acceptance_criteria": DEFAULT_ACCEPTANCE_CRITERIA["docs"],
        "human_checkpoint": False,
    },
}

REFERENCE_FLOW_PRESETS = {
    "lightweight": [
        {"name": "requirements", "stage_type": "requirements", "label": "任务澄清", "role": "需求分析师"},
        {"name": "solution_design", "stage_type": "architecture", "label": "实现方案", "role": "方案设计师"},
        {"name": "implementation", "stage_type": "coding", "label": "编码实现", "role": "软件工程师"},
        {"name": "verification", "stage_type": "testing", "label": "验证测试", "role": "测试工程师"},
    ],
    "standard": [
        {"name": "requirements", "stage_type": "requirements", "label": "需求分析", "role": "需求分析师"},
        {"name": "architecture", "stage_type": "architecture", "label": "架构设计", "role": "架构设计师"},
        {"name": "implementation", "stage_type": "coding", "label": "编码实现", "role": "软件工程师"},
        {"name": "verification", "stage_type": "testing", "label": "测试验证", "role": "测试工程师"},
        {"name": "documentation", "stage_type": "docs", "label": "交付文档", "role": "文档工程师"},
    ],
    "deep": [
        {"name": "requirements", "stage_type": "requirements", "label": "需求拆解", "role": "产品分析师"},
        {"name": "architecture", "stage_type": "architecture", "label": "技术方案", "role": "技术架构师"},
        {"name": "core_implementation", "stage_type": "coding", "label": "核心实现", "role": "主程工程师"},
        {"name": "system_verification", "stage_type": "testing", "label": "系统验证", "role": "测试工程师"},
        {"name": "release_docs", "stage_type": "docs", "label": "交付说明", "role": "文档工程师"},
    ],
}


def normalize_stage_type(stage_type: str | None) -> str:
    raw = str(stage_type or "").strip().lower()
    if not raw:
        return "requirements"
    if raw in STAGE_TYPE_ALIASES:
        return STAGE_TYPE_ALIASES[raw]
    slug = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    if slug in STAGE_TYPE_ALIASES:
        return STAGE_TYPE_ALIASES[slug]
    return slug or "requirements"


def stage_prompt_key(stage_name: str, stage_type: str | None = None) -> str:
    if stage_name in DEFAULT_STAGE_PROMPTS:
        return stage_name
    normalized = normalize_stage_type(stage_type or stage_name)
    return normalized if normalized in DEFAULT_STAGE_PROMPTS else stage_name


def render_stage_prompt(stage_name: str, spec: str, override_prompt: str | None = None, stage_type: str | None = None) -> str:
    prompt_key = stage_prompt_key(stage_name, stage_type=stage_type)
    template = override_prompt or DEFAULT_STAGE_PROMPTS.get(prompt_key, "{spec}")
    try:
        return template.format(spec=spec)
    except Exception:
        return template


def _extract_json_block(text: str) -> Dict[str, Any] | None:
    if not text:
        return None
    raw = text.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


def _slugify_stage_name(value: str | None, fallback: str) -> str:
    raw = str(value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    return slug or fallback


def _stage_blueprint(stage_type: str) -> Dict[str, Any]:
    normalized = normalize_stage_type(stage_type)
    blueprint = dict(STAGE_TYPE_BLUEPRINTS.get(normalized) or STAGE_TYPE_BLUEPRINTS["requirements"])
    blueprint["stage_type"] = normalized
    return blueprint


def _estimate_task_complexity(spec: str) -> str:
    raw = str(spec or "")
    text = raw.lower()
    simple_hits = sum(1 for token in ["简单", "demo", "小游戏", "单页", "脚本", "样例", "mvp", "原型"] if token in raw or token in text)
    complex_hits = sum(1 for token in ["系统", "平台", "工作流", "多角色", "数据库", "权限", "部署", "接口", "后台", "前端", "协同", "langgraph", "agent", "多智能体"] if token in raw or token in text)
    if len(raw) > 220:
        complex_hits += 1
    if len(raw) < 80:
        simple_hits += 1
    if complex_hits >= 3:
        return "complex"
    if simple_hits >= 2 and complex_hits == 0:
        return "simple"
    return "standard"


def _make_stage_instance(stage_type: str, spec: str, name: str | None = None, used_names: set[str] | None = None, **overrides: Any) -> Dict[str, Any]:
    stage = _stage_blueprint(stage_type)
    fallback_name = stage["name"] if isinstance(stage.get("name"), str) else stage_type
    base_name = _slugify_stage_name(name or overrides.get("name") or stage.get("label"), fallback=fallback_name)
    used = used_names if used_names is not None else set()
    unique_name = base_name
    suffix = 2
    while unique_name in used:
        unique_name = f"{base_name}_{suffix}"
        suffix += 1
    used.add(unique_name)
    stage_name = unique_name
    stage["name"] = stage_name
    stage["stage_type"] = normalize_stage_type(stage_type)
    stage["label"] = str(overrides.get("label") or stage.get("label") or stage_name)
    stage["role"] = str(overrides.get("role") or stage.get("role") or f"{stage['stage_type']}-agent")
    prompt_override = overrides.get("prompt_template")
    stage["prompt_template"] = render_stage_prompt(stage_name, spec, prompt_override if isinstance(prompt_override, str) else None, stage_type=stage["stage_type"])
    stage["acceptance_criteria"] = str(overrides.get("acceptance_criteria") or stage.get("acceptance_criteria") or DEFAULT_ACCEPTANCE_CRITERIA.get(stage["stage_type"], ""))
    caps = overrides.get("capabilities")
    stage["capabilities"] = list(caps) if isinstance(caps, list) and caps else list(stage.get("capabilities") or [])
    stage["human_checkpoint"] = bool(overrides.get("human_checkpoint", stage.get("human_checkpoint", False)))
    depends_on = overrides.get("depends_on")
    stage["depends_on"] = [str(dep) for dep in depends_on if dep] if isinstance(depends_on, list) else []
    conversation_group = _normalize_conversation_group(
        overrides.get("conversation_group")
        or overrides.get("group_key")
        or overrides.get("group")
        or overrides.get("loop_group")
        or overrides.get("collaboration_group")
    )
    if conversation_group:
        stage["conversation_group"] = conversation_group
    if stage["stage_type"] == "architecture" and "文件清单" not in str(stage.get("prompt_template") or ""):
        stage["prompt_template"] = (str(stage["prompt_template"]).strip() + ARCHITECTURE_FILE_LIST_HINT).strip()
    return stage


def _ensure_stage_prerequisites(stages: List[Dict[str, Any]], spec: str) -> List[Dict[str, Any]]:
    result = [dict(stage) for stage in stages if isinstance(stage, dict) and stage.get("name")]
    used_names = {str(stage.get("name")) for stage in result if stage.get("name")}

    def has_prior(before_index: int, stage_type: str) -> bool:
        normalized = normalize_stage_type(stage_type)
        return any(normalize_stage_type(stage.get("stage_type") or stage.get("name")) == normalized for stage in result[:before_index])

    def insert_before(before_index: int, stage_type: str, label: str | None = None) -> int:
        if has_prior(before_index, stage_type):
            return before_index
        auto_stage = _make_stage_instance(stage_type, spec, used_names=used_names, label=label)
        result.insert(before_index, auto_stage)
        return before_index + 1

    idx = 0
    while idx < len(result):
        stage_type = normalize_stage_type(result[idx].get("stage_type") or result[idx].get("name"))
        if stage_type == "architecture":
            idx = insert_before(idx, "requirements", label="自动补全需求")
        elif stage_type == "coding":
            idx = insert_before(idx, "requirements", label="自动补全需求")
            idx = insert_before(idx, "architecture", label="自动补全方案")
        elif stage_type == "testing":
            idx = insert_before(idx, "requirements", label="自动补全需求")
            idx = insert_before(idx, "architecture", label="自动补全方案")
            idx = insert_before(idx, "coding", label="自动补全实现")
        elif stage_type == "docs" and any(normalize_stage_type(item.get("stage_type") or item.get("name")) == "coding" for item in result):
            idx = insert_before(idx, "requirements", label="自动补全需求")
            idx = insert_before(idx, "architecture", label="自动补全方案")
            idx = insert_before(idx, "coding", label="自动补全实现")
        idx += 1

    all_names = {str(stage.get("name")) for stage in result if stage.get("name")}
    for i, stage in enumerate(result):
        raw_deps = stage.get("depends_on") if isinstance(stage.get("depends_on"), list) else []
        deps = [str(dep) for dep in raw_deps if dep in all_names and dep != stage.get("name")]
        stage["depends_on"] = deps or ([str(result[i - 1].get("name"))] if i > 0 else [])
        stage["stage_type"] = normalize_stage_type(stage.get("stage_type") or stage.get("name"))
    return result


def _normalize_stage_plan(raw_stages: List[Dict[str, Any]], spec: str) -> List[Dict[str, Any]]:
    planned: List[Dict[str, Any]] = []
    used_names: set[str] = set()
    for idx, item in enumerate(raw_stages or [], start=1):
        if not isinstance(item, dict):
            continue
        stage_type = normalize_stage_type(item.get("stage_type") or item.get("executor_type") or item.get("name") or item.get("label"))
        if stage_type not in STAGE_EXECUTOR_TYPES:
            continue
        stage = _make_stage_instance(
            stage_type,
            spec,
            name=item.get("name") or item.get("id") or item.get("label") or f"{stage_type}_{idx}",
            used_names=used_names,
            label=item.get("label"),
            role=item.get("role"),
            prompt_template=item.get("prompt_template"),
            capabilities=item.get("capabilities"),
            acceptance_criteria=item.get("acceptance_criteria"),
            human_checkpoint=item.get("human_checkpoint"),
            depends_on=item.get("depends_on"),
            conversation_group=item.get("conversation_group") or item.get("group_key") or item.get("group") or item.get("loop_group") or item.get("collaboration_group"),
        )
        planned.append(stage)
    return _ensure_stage_prerequisites(planned, spec) if planned else []


def resolve_conversation_groups(stages: List[Dict[str, Any]], raw_groups: Any = None) -> List[Dict[str, Any]]:
    normalized_stages = [stage for stage in (stages or []) if isinstance(stage, dict) and stage.get("name")]
    stage_names = [str(stage.get("name")) for stage in normalized_stages if stage.get("name")]
    stage_map = {str(stage.get("name")): stage for stage in normalized_stages if stage.get("name")}
    groups: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    grouped_stage_names: set[str] = set()

    def add_group(key: str, label: str, members: List[str], kind: str = "stage_flow") -> None:
        clean_members = [name for name in members if name in stage_map]
        if not key or not clean_members or key in seen_keys:
            return
        seen_keys.add(key)
        groups.append({
            "key": key,
            "label": label or key,
            "kind": kind or "stage_flow",
            "stage_names": clean_members,
        })
        grouped_stage_names.update(clean_members)

    if isinstance(raw_groups, list):
        for item in raw_groups:
            normalized = _normalize_conversation_group(item)
            if not normalized or not isinstance(item, dict):
                continue
            raw_members = item.get("stage_names") or item.get("stages") or item.get("members") or []
            members = [str(name) for name in raw_members if str(name) in stage_map] if isinstance(raw_members, list) else []
            add_group(normalized["key"], normalized["label"], members, str(normalized.get("kind") or "stage_flow"))

    for stage in normalized_stages:
        group_cfg = _normalize_conversation_group(
            stage.get("conversation_group")
            or stage.get("group_key")
            or stage.get("group")
            or stage.get("loop_group")
            or stage.get("collaboration_group")
        )
        if not group_cfg:
            continue
        existing = next((group for group in groups if group["key"] == group_cfg["key"]), None)
        if existing:
            if stage["name"] not in existing["stage_names"]:
                existing["stage_names"].append(stage["name"])
                grouped_stage_names.add(stage["name"])
            continue
        add_group(group_cfg["key"], group_cfg["label"], [stage["name"]], str(group_cfg.get("kind") or "stage_flow"))

    current_chain: List[str] = []
    for stage in normalized_stages:
        stage_name = str(stage["name"])
        if stage_name in grouped_stage_names:
            if len(current_chain) > 1:
                add_group(
                    f"flow:{current_chain[0]}:{current_chain[-1]}",
                    "开发闭环",
                    current_chain[:],
                    "loop",
                )
            current_chain = []
            continue
        stage_type = normalize_stage_type(stage.get("stage_type") or stage_name)
        if stage_type in {"coding", "testing"}:
            current_chain.append(stage_name)
        else:
            if len(current_chain) > 1:
                add_group(
                    f"flow:{current_chain[0]}:{current_chain[-1]}",
                    "开发闭环",
                    current_chain[:],
                    "loop",
                )
            current_chain = []
    if len(current_chain) > 1:
        add_group(
            f"flow:{current_chain[0]}:{current_chain[-1]}",
            "开发闭环",
            current_chain[:],
            "loop",
        )

    for stage_name in stage_names:
        if stage_name in grouped_stage_names:
            continue
        stage = stage_map[stage_name]
        add_group(stage_name, str(stage.get("label") or stage_name), [stage_name], "stage")

    group_index = {group["key"]: group for group in groups}
    for stage in normalized_stages:
        stage_name = str(stage["name"])
        matched = next((group for group in groups if stage_name in group["stage_names"]), None)
        if matched:
            stage["conversation_group"] = {
                "key": matched["key"],
                "label": matched["label"],
                "kind": matched.get("kind") or "stage_flow",
            }
    return groups


def write_leader_plan_snapshot(task: Task, base_dir: str, leader_plan: Dict[str, Any]) -> str:
    workspace = os.path.abspath(task.workspace_path or os.path.join(base_dir, task.task_id))
    plan_dir = os.path.join(workspace, "plan")
    os.makedirs(plan_dir, exist_ok=True)
    plan_path = os.path.join(plan_dir, "leader_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(leader_plan or {}, f, ensure_ascii=False, indent=2)
    return plan_path


def _build_fallback_plan(spec: str) -> Dict[str, Any]:
    complexity = _estimate_task_complexity(spec)
    preset_name = "lightweight" if complexity == "simple" else "deep" if complexity == "complex" else "standard"
    stages = _normalize_stage_plan(REFERENCE_FLOW_PRESETS.get(preset_name, []), spec)
    summary_map = {
        "simple": "任务偏简单，采用精简流程，避免过度设计。",
        "standard": "任务复杂度中等，采用需求→方案→实现→测试→交付的标准闭环。",
        "complex": "任务复杂度较高，采用更稳健的多阶段研发流程。",
    }
    return {
        "complexity": complexity,
        "reference_preset": preset_name,
        "summary": summary_map.get(complexity, "采用标准执行流程。"),
        "stages": stages,
    }


def _infer_provider_type(base_url: str, model_name: str = "") -> str:
    base = str(base_url or "").lower()
    model = str(model_name or "").lower()
    if "generativelanguage.googleapis.com" in base or "/v1beta" in base and "googleapis" in base:
        return "gemini"
    if "codex" in base or "codex" in model or model.startswith("gpt-5"):
        return "codex"
    if model.startswith("gemini"):
        return "gemini"
    return "openai-compatible"


def _registry_provider_cfg(provider_id: str, model_row: Dict[str, Any], cred: Dict[str, Any], overrides: Dict[str, Any] | None = None) -> Dict[str, Any]:
    inferred_type = _infer_provider_type(cred.get("base_url") or "", model_row.get("name") or "")
    provider_type = model_row.get("provider_type") or inferred_type
    if provider_type == "openai-compatible" and inferred_type == "codex":
        provider_type = "codex"
    cfg = {
        "id": provider_id,
        "type": provider_type,
        "model": model_row.get("name") or "",
        "base_url": cred.get("base_url") or "",
        "api_key_env": cred.get("api_key_env") or None,
        "api_key": cred.get("api_key") or None,
        "label": f"{cred.get('name') or 'Registry'} / {model_row.get('name') or ''}",
    }
    extra = model_row.get("extra_config") or {}
    if isinstance(extra, dict):
        cfg.update({k: v for k, v in extra.items() if v not in (None, "")})
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None and v != ""})
    return cfg


class ReqAgent:
    def __init__(self, model_adapter, stage_name: str = "requirements", stage_type: str = "requirements", prompt_template: str | None = None, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.model_adapter = model_adapter
        self.stage_name = stage_name
        self.stage_type = normalize_stage_type(stage_type)
        self.prompt_template = prompt_template
        self.progress_callback = progress_callback
        self.id = "req-analyst"
        self.capabilities = ["analysis.requirements:v1"]

    def _generate_text(self, task: Task, prompt: str, action_label: str) -> str:
        _emit_stage_progress(
            self.progress_callback,
            progress_kind="model",
            progress_state="start",
            message=action_label,
        )
        try:
            text = str(self.model_adapter.generate(prompt, context=task.context))
        except Exception as exc:
            _emit_stage_progress(
                self.progress_callback,
                progress_kind="model",
                progress_state="error",
                message=f"{action_label}失败",
                error=str(exc),
            )
            raise
        failure = _model_failure_text(text)
        _emit_stage_progress(
            self.progress_callback,
            progress_kind="model",
            progress_state="error" if failure else "done",
            message=f"{action_label}{'失败' if failure else '完成'}",
            error=failure or None,
        )
        return text

    def act(self, task: Task, state: SystemState):
        prompt = render_stage_prompt(self.stage_name, task.context.get("spec", ""), self.prompt_template, stage_type=self.stage_type)
        prompt = (prompt.rstrip() + TEXT_OUTPUT_QUALITY_GUARDRAIL).strip()
        prompt = append_prompt_with_runtime_context(prompt, task, self.stage_name)
        text = self._generate_text(task, prompt, "正在生成需求文档")
        failure = _model_failure_text(text)
        if failure:
            raise ValueError(f"requirements_model_failed:{failure[:240]}")
        return {"type": "md", "filename": "analysis/requirements.md", "content": text}


class ArchAgent:
    def __init__(self, model_adapter, stage_name: str = "architecture", stage_type: str = "architecture", prompt_template: str | None = None, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.model_adapter = model_adapter
        self.stage_name = stage_name
        self.stage_type = normalize_stage_type(stage_type)
        self.prompt_template = prompt_template
        self.progress_callback = progress_callback
        self.id = "architect"
        self.capabilities = ["design.arch:v1"]

    def _generate_text(self, task: Task, prompt: str, action_label: str) -> str:
        _emit_stage_progress(
            self.progress_callback,
            progress_kind="model",
            progress_state="start",
            message=action_label,
        )
        try:
            text = str(self.model_adapter.generate(prompt, context=task.context))
        except Exception as exc:
            _emit_stage_progress(
                self.progress_callback,
                progress_kind="model",
                progress_state="error",
                message=f"{action_label}失败",
                error=str(exc),
            )
            raise
        failure = _model_failure_text(text)
        _emit_stage_progress(
            self.progress_callback,
            progress_kind="model",
            progress_state="error" if failure else "done",
            message=f"{action_label}{'失败' if failure else '完成'}",
            error=failure or None,
        )
        return text

    def _load_requirements_text(self, task: Task) -> str:
        return _load_task_artifact_text(task, os.path.join("analysis", "requirements.md"))

    def _stack_guardrail(self, spec: str, requirements_text: str, prompt: str) -> str:
        project_stack = _infer_declared_stack(requirements_text) or _infer_project_stack(spec, requirements_text, prompt)
        stack_hint = "Web（HTML/CSS/JS/TS）" if project_stack == "web" else "Python"
        stack_forbidden = (
            "不要出现 Python 文件、requirements.txt、pyproject.toml 等 Python 项目清单。"
            if project_stack == "web"
            else "不要出现 package.json、vite.config.*、src/*.ts、index.html 等前端工程清单。"
        )
        return (
            "\n\n硬性约束：\n"
            f"- 本阶段只能选择并落定一套最终技术栈，优先对齐需求文档的运行形态；当前应输出 {stack_hint} 方案。\n"
            "- 文档中只能保留一个标题严格为“## 文件清单”的章节。\n"
            "- 文件清单中的每一行都必须是一个相对路径，禁止在同一文档中混入第二套语言/框架的文件路径。\n"
            f"- {stack_forbidden}\n"
            "- 不要在文件清单章节之外再追加另一份“补充文件列表”或历史残留清单。"
        )

    def _ensure_file_list_section(self, task: Task, spec: str, arch_text: str) -> str:
        requirements_text = self._load_requirements_text(task)
        return _normalize_architecture_markdown(spec, requirements_text, arch_text)

    def act(self, task: Task, state: SystemState):
        spec = task.context.get("spec", "")
        requirements_text = self._load_requirements_text(task)
        prompt = render_stage_prompt(self.stage_name, spec, self.prompt_template, stage_type=self.stage_type)
        if "文件清单" not in prompt:
            prompt = (prompt.rstrip() + ARCHITECTURE_FILE_LIST_HINT).strip()
        prompt = (prompt.rstrip() + self._stack_guardrail(spec, requirements_text, prompt) + TEXT_OUTPUT_QUALITY_GUARDRAIL).strip()
        prompt = append_prompt_with_runtime_context(prompt, task, self.stage_name)
        text = self._generate_text(task, prompt, "正在生成架构方案")
        failure = _model_failure_text(text)
        if failure:
            raise ValueError(f"architecture_model_failed:{failure[:240]}")
        text = self._ensure_file_list_section(task, spec, text)
        return {"type": "md", "filename": "design/architecture.md", "content": text}


class DocAgent:
    def __init__(self, model_adapter, stage_name: str = "docs", stage_type: str = "docs", prompt_template: str | None = None, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.model_adapter = model_adapter
        self.stage_name = stage_name
        self.stage_type = normalize_stage_type(stage_type)
        self.prompt_template = prompt_template
        self.progress_callback = progress_callback
        self.id = "doc-writer"
        self.capabilities = ["doc.write:v1"]

    def _generate_text(self, task: Task, prompt: str, action_label: str) -> str:
        _emit_stage_progress(
            self.progress_callback,
            progress_kind="model",
            progress_state="start",
            message=action_label,
        )
        try:
            text = str(self.model_adapter.generate(prompt, context=task.context))
        except Exception as exc:
            _emit_stage_progress(
                self.progress_callback,
                progress_kind="model",
                progress_state="error",
                message=f"{action_label}失败",
                error=str(exc),
            )
            raise
        failure = _model_failure_text(text)
        _emit_stage_progress(
            self.progress_callback,
            progress_kind="model",
            progress_state="error" if failure else "done",
            message=f"{action_label}{'失败' if failure else '完成'}",
            error=failure or None,
        )
        return text

    def act(self, task: Task, state: SystemState):
        prompt = render_stage_prompt(self.stage_name, task.context.get("spec", ""), self.prompt_template, stage_type=self.stage_type)
        prompt = (prompt.rstrip() + TEXT_OUTPUT_QUALITY_GUARDRAIL).strip()
        prompt = append_prompt_with_runtime_context(prompt, task, self.stage_name)
        text = self._generate_text(task, prompt, "正在生成交付文档")
        failure = _model_failure_text(text)
        if failure:
            raise ValueError(f"docs_model_failed:{failure[:240]}")
        return {"type": "md", "filename": "docs/README.md", "content": text}


class GraphBuilder:
    def __init__(self, base_dir: str, model_registry: ModelRegistry, storage=None):
        self.base_dir = base_dir
        self.model_registry = model_registry
        self.storage = storage or FileStore()

    def plan_workflow(self, task: Task, template: Dict[str, Any]) -> Dict[str, Any]:
        """Leader planning stage: dynamically design workflow per task."""
        planner_model = self._select_model(task, stage_name="planning", capabilities=["planning.workflow:v1"])
        spec = str((task.context or {}).get("spec") or "")
        reference_stages = []
        for item in template.get("stages", []) or []:
            if not isinstance(item, dict):
                continue
            normalized = _make_stage_instance(
                item.get("stage_type") or item.get("name") or "requirements",
                spec,
                name=item.get("name"),
                label=item.get("label"),
                role=item.get("role"),
                capabilities=item.get("capabilities"),
                prompt_template=item.get("prompt_template"),
                acceptance_criteria=item.get("acceptance_criteria"),
                human_checkpoint=item.get("human_checkpoint"),
            )
            reference_stages.append({
                "name": normalized.get("name"),
                "stage_type": normalized.get("stage_type"),
                "label": normalized.get("label"),
                "role": normalized.get("role"),
                "capabilities": normalized.get("capabilities"),
            })

        fallback_plan = _build_fallback_plan(spec)
        preset_view = {
            name: [
                {
                    "name": item.get("name"),
                    "stage_type": normalize_stage_type(item.get("stage_type") or item.get("name")),
                    "label": item.get("label"),
                    "role": item.get("role"),
                }
                for item in items
            ]
            for name, items in REFERENCE_FLOW_PRESETS.items()
        }
        blueprint_view = {
            stage_type: {
                "label": blueprint.get("label"),
                "role": blueprint.get("role"),
                "capabilities": blueprint.get("capabilities"),
                "default_prompt_template": blueprint.get("prompt_template"),
                "default_acceptance_criteria": blueprint.get("acceptance_criteria"),
            }
            for stage_type, blueprint in STAGE_TYPE_BLUEPRINTS.items()
        }
        plan_prompt = (
            "你是项目的管理者/智者智能体，负责根据任务复杂度现场设计执行流程。\n"
            "你的职责不是套固定模板，而是根据任务目标、风险、规模，决定需要哪些角色、哪些阶段、阶段顺序、阶段数量以及每个阶段的提示词。\n"
            f"任务需求：{spec}\n"
            f"参考预设（只作参考，不能机械照搬）：{json.dumps(preset_view, ensure_ascii=False)}\n"
            f"参考阶段蓝图（stage_type 必须从这里选择或映射）：{json.dumps(blueprint_view, ensure_ascii=False)}\n"
            f"现有标准流程仅供参考：{json.dumps(reference_stages, ensure_ascii=False)}\n"
            "设计要求：\n"
            "1) 你可以自定义阶段 name/label/role，也可以重复同一种 stage_type 形成开发闭环；\n"
            "2) stage_type 必须可落到执行器，只能是 requirements / architecture / coding / testing / docs 之一（允许你先用别名描述，但最终 JSON 里请填规范值）；\n"
            "3) 简单任务要避免过度拆解，复杂任务可以拆出多个 implementation / verification 闭环；\n"
            "4) 只要存在 coding 类阶段，前面必须至少有一个 requirements 和一个 architecture 类阶段；\n"
            "5) 只要存在 testing 类阶段，前面必须至少有一个 coding 类阶段；\n"
            "6) prompt_template 必须可以直接喂给对应执行智能体；\n"
            "7) 如存在跨阶段协作，请输出 conversation_groups，或在阶段上附 conversation_group；\n"
            "8) 只输出严格 JSON，不要解释。\n"
            "输出格式："
            "{\"complexity\":\"simple|standard|complex\",\"reference_preset\":\"lightweight|standard|deep|custom\",\"summary\":\"...\",\"conversation_groups\":[{\"key\":\"dev_loop\",\"label\":\"开发闭环\",\"kind\":\"loop\",\"stages\":[\"core_impl\",\"qa_verification\"]}],\"stages\":[{\"name\":\"clarify_scope\",\"stage_type\":\"requirements\",\"label\":\"范围澄清\",\"role\":\"产品分析师\",\"prompt_template\":\"...\",\"capabilities\":[\"analysis.requirements:v1\"],\"acceptance_criteria\":\"...\",\"depends_on\":[],\"human_checkpoint\":false},{\"name\":\"core_impl\",\"stage_type\":\"coding\",\"label\":\"核心实现\",\"role\":\"软件工程师\",\"conversation_group\":{\"key\":\"dev_loop\",\"label\":\"开发闭环\"},\"prompt_template\":\"...\",\"capabilities\":[\"code.edit:v1\"],\"acceptance_criteria\":\"...\",\"depends_on\":[\"clarify_scope\"],\"human_checkpoint\":false}]}"
        )

        try:
            out = planner_model.generate(plan_prompt, context=task.context)
            parsed = _extract_json_block(out)
        except Exception as exc:
            out = f"[planning error] {exc}"
            parsed = None

        planned_stages = _normalize_stage_plan(parsed.get("stages") if isinstance(parsed, dict) else [], spec)
        used_fallback = not bool(planned_stages)
        if used_fallback:
            planned_stages = fallback_plan["stages"]

        event_configs = task.context.setdefault("event_configs", {})
        for stage in planned_stages:
            stage_name = str(stage.get("name") or "").strip()
            if not stage_name:
                continue
            stage_type = normalize_stage_type(stage.get("stage_type") or stage_name)
            base_cfg = dict(event_configs.get(stage_type, {})) if stage_type != stage_name else dict(event_configs.get(stage_name, {}))
            stage_cfg = dict(base_cfg)
            stage_cfg.update(event_configs.get(stage_name, {}))
            stage_cfg["stage_type"] = stage_type
            if stage.get("prompt_template"):
                stage_cfg["prompt_template"] = stage["prompt_template"]
            if stage.get("role"):
                stage_cfg["planned_role"] = stage["role"]
            if stage.get("acceptance_criteria"):
                stage_cfg["acceptance_criteria"] = stage["acceptance_criteria"]
            event_configs[stage_name] = stage_cfg

        plan_meta = parsed if isinstance(parsed, dict) else {}
        conversation_groups = resolve_conversation_groups(planned_stages, plan_meta.get("conversation_groups"))
        task.context["event_configs"] = event_configs
        task.context["leader_plan"] = {
            "complexity": str(plan_meta.get("complexity") or fallback_plan.get("complexity") or "standard"),
            "reference_preset": str(plan_meta.get("reference_preset") or fallback_plan.get("reference_preset") or "custom"),
            "summary": str(plan_meta.get("summary") or fallback_plan.get("summary") or ""),
            "stages": planned_stages,
            "conversation_groups": conversation_groups,
            "raw_output": out,
            "used_fallback": used_fallback,
        }
        write_leader_plan_snapshot(task, self.base_dir, task.context["leader_plan"])
        return {
            "complexity": task.context["leader_plan"].get("complexity"),
            "reference_preset": task.context["leader_plan"].get("reference_preset"),
            "summary": task.context["leader_plan"].get("summary"),
            "conversation_groups": task.context["leader_plan"].get("conversation_groups"),
            "stages": planned_stages,
        }

    def load_template(self, template_path: str) -> Dict[str, Any]:
        with open(template_path, "r") as f:
            return json.load(f)

    def load_agents(self, agents_yaml: str) -> Dict[str, Dict[str, Any]]:
        with open(agents_yaml, "r") as f:
            data = yaml.safe_load(f)
        return {a["id"]: a for a in data.get("agents", [])}

    def _select_model(self, task: Task, stage_name: str | None = None, capabilities: List[str] | None = None):
        context = task.context or {}
        event_configs = context.get("event_configs") or {}
        stage_cfg = event_configs.get(stage_name, {}) if stage_name else {}
        explicit_provider = (
            stage_cfg.get("model_provider")
            or context.get("default_model_provider")
        )
        model_overrides = {
            "model": stage_cfg.get("model"),
            "temperature": stage_cfg.get("temperature"),
            "timeout": stage_cfg.get("timeout"),
            "base_url": stage_cfg.get("base_url"),
            "api_key_env": stage_cfg.get("api_key_env"),
            "api_key": stage_cfg.get("api_key"),
        }
        has_overrides = any(v is not None and v != "" for v in model_overrides.values())

        if explicit_provider:
            if str(explicit_provider).startswith("registry:model:"):
                model_id = str(explicit_provider).split(":", 2)[-1]
                model_row = db.get_ai_model(model_id)
                cred = db.get_ai_credential_secret((model_row or {}).get("credential_id", "")) if model_row else None
                if model_row and cred:
                    cfg = _registry_provider_cfg(explicit_provider, model_row, cred, model_overrides if has_overrides else None)
                    return self.model_registry.build_adapter(cfg)
            return self.model_registry.get_by_id(explicit_provider, overrides=model_overrides if has_overrides else None)

        llm_models = [m for m in db.list_ai_models() if (m.get("model_kind") or "llm") == "llm"]
        if llm_models:
            model_id = llm_models[0].get("model_id", "")
            model_row = db.get_ai_model(model_id)
            cred = db.get_ai_credential_secret((model_row or {}).get("credential_id", "")) if model_row else None
            if model_row and cred:
                cfg = _registry_provider_cfg(f"registry:model:{model_id}", model_row, cred, model_overrides if has_overrides else None)
                return self.model_registry.build_adapter(cfg)
        raise ValueError("未配置可用的 llm 模型，请先到 /models.html 注册并绑定模型")

    def _review_stage_output(self, task: Task, stage_name: str, payload: Dict[str, Any], stage_type: str | None = None, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        effective_stage_type = normalize_stage_type(stage_type or stage_name)
        event_configs = (task.context or {}).get("event_configs", {})
        stage_cfg = dict(event_configs.get(effective_stage_type, {})) if effective_stage_type != stage_name else {}
        stage_cfg.update(event_configs.get(stage_name, {}) if isinstance(event_configs, dict) else {})
        criteria = str(stage_cfg.get("acceptance_criteria") or "").strip()
        role = str(stage_cfg.get("planned_role") or "")
        summary = payload.get("output_summary") or {}
        artifacts = payload.get("artifacts") or []
        review_text_parts: List[str] = []
        total_chars = 0
        max_chars = 24000
        architecture_doc_text = ""
        docs_readme_text = ""
        workspace = os.path.abspath(task.workspace_path or os.path.join(self.base_dir, task.task_id))
        evidence_paths: List[str] = []
        seen_evidence = set()

        def add_evidence_path(path: str) -> None:
            abs_path = os.path.abspath(path)
            if abs_path in seen_evidence:
                return
            seen_evidence.add(abs_path)
            evidence_paths.append(abs_path)

        if effective_stage_type == "docs":
            add_evidence_path(os.path.join(workspace, "docs", "README.md"))
            add_evidence_path(os.path.join(workspace, "tests", "manual_test_report.md"))
            add_evidence_path(os.path.join(workspace, "analysis", "requirements.md"))
            add_evidence_path(os.path.join(workspace, "design", "architecture.md"))

        for art in artifacts[:12]:
            uri = str((art or {}).get("uri") or "")
            if not uri or uri == "inline":
                continue
            add_evidence_path(uri)

        for abs_uri in evidence_paths:
            if not abs_uri.startswith(workspace):
                continue
            low = abs_uri.lower()
            if not (
                low.endswith(".md")
                or low.endswith(".txt")
                or low.endswith(".py")
                or low.endswith(".json")
                or low.endswith(".html")
                or low.endswith(".css")
                or low.endswith(".js")
                or low.endswith(".ts")
                or low.endswith(".tsx")
                or low.endswith(".jsx")
            ):
                continue
            try:
                with open(abs_uri, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read(12000)
            except Exception:
                continue
            if not text:
                continue
            if effective_stage_type == "architecture" and abs_uri.lower().endswith("architecture.md"):
                architecture_doc_text = text
            if effective_stage_type == "docs" and abs_uri.lower().endswith(os.path.join("docs", "readme.md")):
                docs_readme_text = text
            left = max_chars - total_chars
            if left <= 0:
                break
            clipped = text[:left]
            review_text_parts.append(f"[{os.path.relpath(abs_uri, workspace)}]\n{clipped}")
            total_chars += len(clipped)

        review_text = "\n\n".join(review_text_parts)
        collaboration_text = CollaborationHub(task).build_stage_review_context(stage_name, local_limit=4, blackboard_limit=4, max_chars=3000)
        agent_decision_candidate = _extract_agent_decision_candidates(payload)
        smoke_failed = self._artifact_command_failed(payload, {"smoke_test_result"})
        test_failed = self._artifact_command_failed(payload, {"test_result", "compile_result"})
        validation_signals: List[str] = []
        architecture_issues: List[str] = []
        docs_issues: List[str] = []
        if effective_stage_type == "coding":
            validation_signals.append(f"编码阶段冒烟结果：{'失败' if smoke_failed else '通过'}")
        if effective_stage_type == "testing":
            validation_signals.append(f"测试阶段执行结果：{'失败' if test_failed else '通过'}")
        if effective_stage_type == "architecture" and architecture_doc_text:
            requirements_text = _load_task_artifact_text(task, os.path.join("analysis", "requirements.md"))
            architecture_issues = _architecture_validation_issues(
                str((task.context or {}).get("spec") or ""),
                requirements_text,
                architecture_doc_text,
            )
            if architecture_issues:
                validation_signals.append("架构文档结构校验未通过：" + "；".join(architecture_issues))
        if effective_stage_type == "docs" and docs_readme_text:
            docs_issues = _docs_validation_issues(docs_readme_text)
            if docs_issues:
                validation_signals.append("README 结构校验未通过：" + "；".join(docs_issues))
            else:
                validation_signals.append("README 结构校验通过：已检测到运行方式、文件结构、限制说明与测试结论。")
        if review_text_parts:
            validation_signals.append("注意：下方文件片段可能因长度限制被截断，不能仅凭片段结尾不完整就认定源文件本身被截断；应结合编译/测试结果综合判断。")

        if effective_stage_type == "testing":
            has_manual_report = any(str((art or {}).get("uri") or "").endswith("manual_test_report.md") for art in artifacts)
            workspace = os.path.abspath(task.workspace_path or os.path.join(self.base_dir, task.task_id))
            source_files: List[str] = []
            for root_dir, _, file_names in os.walk(workspace):
                for file_name in file_names:
                    if file_name.endswith((".py", ".html", ".css", ".js", ".ts", ".tsx", ".jsx")):
                        source_files.append(os.path.join(root_dir, file_name))
            manual_report_missing_code = (
                "未发现 Python 源文件" in review_text
                or "未发现可执行源码文件" in review_text
            )
            if has_manual_report and source_files and not manual_report_missing_code and not self._artifact_command_failed(payload, {"test_result", "compile_result"}):
                has_web_source = any(path.endswith((".html", ".css", ".js", ".ts", ".tsx", ".jsx")) for path in source_files)
                feedback = "未发现可执行自动化用例，已按回退策略完成源码编译校验并生成手工测试清单，本轮测试阶段可暂时验收。"
                risks = [
                    "当前仍以编译校验和手工测试清单为主，后续迭代建议补充自动化测试。",
                    "UI 与交互体验仍需人工走查确认。",
                ]
                next_actions = [
                    "按 manual_test_report.md 执行关键玩法与 UI 手工验收。",
                    "后续补充至少一组核心逻辑自动化测试用例。",
                ]
                if has_web_source:
                    feedback = "未发现可执行自动化用例，已按回退策略完成 Web 静态校验并生成手工测试清单，本轮测试阶段可暂时验收。"
                    risks = [
                        "当前仍以静态校验和手工测试清单为主，真实浏览器交互仍需人工确认。",
                        "后续迭代建议补充浏览器侧自动化冒烟或交互测试。",
                    ]
                    next_actions = [
                        "按 manual_test_report.md 执行关键玩法、交互与控制台错误手工验收。",
                        "后续补充至少一组浏览器侧自动化测试或脚本化冒烟验证。",
                    ]
                return {
                    "review_status": "fallback",
                    "pass": True,
                    "score": 0.72,
                    "feedback": feedback,
                    "risks": risks,
                    "next_actions": next_actions,
                    "criteria": criteria,
                    "role": role,
                }

        if not criteria:
            return {
                "review_status": "skipped",
                "pass": None,
                "score": None,
                "feedback": "未配置验收标准，跳过自动评审。",
                "criteria": "",
                "role": role,
            }

        review_prompt = (
            "你是团队Leader，负责评审阶段产出是否满足验收标准。请仅输出 JSON。\n"
            f"阶段：{stage_name}\n"
            f"阶段类型：{effective_stage_type}\n"
            f"角色：{role or '-'}\n"
            f"验收标准：{criteria}\n"
            f"验证信号：{json.dumps(validation_signals, ensure_ascii=False)}\n"
            f"阶段执行方提出的人工决策候选：{json.dumps(agent_decision_candidate or {}, ensure_ascii=False)}\n"
            f"阶段输出摘要：{json.dumps(summary, ensure_ascii=False)}\n"
            f"产物：{json.dumps(artifacts[:8], ensure_ascii=False)}\n"
            f"阶段关键内容片段：\n{review_text[:24000]}\n"
            f"阶段协作记录：\n{collaboration_text[:5000]}\n"
            "如果需要人工决策，请把 human_decision_required 设为 true，并补充 decision_question、decision_options、decision_reason；"
            "否则 human_decision_required 设为 false。\n"
            "输出格式："
            "{\"pass\":true,\"score\":0.0,\"feedback\":\"...\",\"risks\":[\"...\"],\"next_actions\":[\"...\"],\"human_decision_required\":false,\"decision_question\":\"...\",\"decision_options\":[\"...\"],\"decision_reason\":\"...\"}"
        )

        raw = ""
        try:
            reviewer = self._select_model(task, stage_name="planning", capabilities=["planning.review:v1"])
            _emit_stage_progress(
                progress_callback,
                progress_kind="review",
                progress_state="start",
                message="正在进行阶段评审",
            )
            raw = str(reviewer.generate(review_prompt, context=task.context))
            failure = _model_failure_text(raw)
            _emit_stage_progress(
                progress_callback,
                progress_kind="review",
                progress_state="error" if failure else "done",
                message=f"阶段评审{'失败' if failure else '完成'}",
                error=failure or None,
            )
            parsed = _extract_json_block(raw) or {}
        except Exception as e:
            parsed = {}
            raw = f"[review error] {e}"
            _emit_stage_progress(
                progress_callback,
                progress_kind="review",
                progress_state="error",
                message="阶段评审失败",
                error=str(e),
            )

        if isinstance(parsed, dict) and isinstance(parsed.get("pass"), bool):
            if architecture_issues:
                merged_feedback = str(parsed.get("feedback") or "").strip()
                parsed["pass"] = False
                parsed["feedback"] = (
                    ((merged_feedback + "\n\n") if merged_feedback else "")
                    + "架构文档存在确定性的结构问题："
                    + "；".join(architecture_issues)
                )
                risks = parsed.get("risks") if isinstance(parsed.get("risks"), list) else []
                for issue in architecture_issues:
                    if issue not in risks:
                        risks.append(issue)
                parsed["risks"] = risks
            if effective_stage_type == "docs":
                merged_feedback = str(parsed.get("feedback") or "").strip()
                if docs_issues:
                    parsed["pass"] = False
                    parsed["feedback"] = (
                        ((merged_feedback + "\n\n") if merged_feedback else "")
                        + "README 存在确定性的结构问题："
                        + "；".join(docs_issues)
                    )
                    risks = parsed.get("risks") if isinstance(parsed.get("risks"), list) else []
                    for issue in docs_issues:
                        if issue not in risks:
                            risks.append(issue)
                    parsed["risks"] = risks
                elif parsed.get("pass") is False and _review_feedback_is_evidence_limited(merged_feedback):
                    parsed["pass"] = True
                    parsed["feedback"] = (
                        "已基于 README 正文执行确定性结构校验，确认其覆盖运行方式、文件结构、限制说明与测试结论；"
                        "本轮将“证据不足型误判”自动纠正为通过。"
                        + ((f"\n\n原评审反馈：{merged_feedback}") if merged_feedback else "")
                    )
            return {
                "review_status": "ok",
                "pass": parsed.get("pass"),
                "score": parsed.get("score"),
                "feedback": parsed.get("feedback", ""),
                "risks": parsed.get("risks", []),
                "next_actions": parsed.get("next_actions", []),
                "human_decision_required": parsed.get("human_decision_required") is True,
                "decision_question": parsed.get("decision_question") or "",
                "decision_options": parsed.get("decision_options", []),
                "decision_reason": parsed.get("decision_reason") or "",
                "criteria": criteria,
                "role": role,
                "raw": raw[:1200],
            }

        artifact_count = int(summary.get("artifact_count") or 0)
        guessed_pass = artifact_count > 0 and not architecture_issues
        return {
            "review_status": "fallback",
            "pass": guessed_pass,
            "score": 0.55 if guessed_pass else 0.25,
            "feedback": (
                "评审模型返回非 JSON，使用启发式评估（按产物数量）。"
                if not architecture_issues
                else "评审模型返回非 JSON，且架构文档存在确定性的结构问题：" + "；".join(architecture_issues)
            ),
            "criteria": criteria,
            "role": role,
            "raw": raw[:1200],
        }

    def _cleanup_artifacts(self, task: Task, artifacts: List[Dict[str, Any]]):
        workspace = os.path.abspath(task.workspace_path or os.path.join(self.base_dir, task.task_id))
        for art in artifacts or []:
            uri = str((art or {}).get("uri") or "")
            if not uri or uri == "inline":
                continue
            abs_uri = os.path.abspath(uri)
            if not abs_uri.startswith(workspace):
                continue
            try:
                if os.path.isdir(abs_uri):
                    shutil.rmtree(abs_uri, ignore_errors=True)
                elif os.path.exists(abs_uri):
                    os.remove(abs_uri)
            except Exception:
                continue

    def _testing_failed(self, payload: Dict[str, Any]) -> bool:
        return self._artifact_command_failed(payload, {"test_result", "compile_result"})

    def _smoke_test_failed(self, payload: Dict[str, Any]) -> bool:
        return self._artifact_command_failed(payload, {"smoke_test_result"})

    def _artifact_command_failed(self, payload: Dict[str, Any], artifact_types: set[str]) -> bool:
        for art in (payload.get("artifacts") or []):
            if (art or {}).get("type") not in artifact_types:
                continue
            content = (art or {}).get("content") or {}
            if isinstance(content, dict):
                exit_code = content.get("exit_code")
                try:
                    if exit_code is not None and int(exit_code) != 0:
                        return True
                except Exception:
                    return True
        return False

    def _collect_test_feedback(self, payload: Dict[str, Any]) -> str:
        return self._collect_command_feedback(payload, {"test_result", "compile_result"})

    def _collect_smoke_feedback(self, payload: Dict[str, Any]) -> str:
        return self._collect_command_feedback(payload, {"smoke_test_result"})

    def _collect_command_feedback(self, payload: Dict[str, Any], artifact_types: set[str]) -> str:
        parts: List[str] = []
        for art in (payload.get("artifacts") or []):
            if (art or {}).get("type") not in artifact_types:
                continue
            content = (art or {}).get("content") or {}
            if not isinstance(content, dict):
                continue
            cmd = str(content.get("command") or "")
            stderr = str(content.get("stderr") or "")[:2000]
            stdout = str(content.get("stdout") or "")[:1000]
            code = content.get("exit_code")
            parts.append(f"cmd={cmd} exit={code}\nstderr={stderr}\nstdout={stdout}")
        return "\n\n".join(parts)[:5000]

    def build(self, task: Task, template: Dict[str, Any], stage_logger=None, should_abort: Callable[[Task], bool] | None = None) -> StateGraph:
        """Build a LangGraph flow from a dynamic stage plan."""
        sg = StateGraph(dict)
        stages = [dict(st) for st in (template.get("stages") or []) if isinstance(st, dict) and st.get("name")]
        if not stages:
            raise ValueError("workflow has no stages")

        stage_defs_by_name = {str(st["name"]): dict(st) for st in stages}
        ordered_stage_names = [str(st["name"]) for st in stages]
        stage_types_by_name = {name: normalize_stage_type(stage_defs_by_name[name].get("stage_type") or name) for name in ordered_stage_names}
        stage_labels = {name: stage_defs_by_name[name].get("label", name) for name in ordered_stage_names}
        stage_caps = {name: stage_defs_by_name[name].get("capabilities", []) for name in ordered_stage_names}
        stages_by_type: Dict[str, List[str]] = {}
        for name in ordered_stage_names:
            stages_by_type.setdefault(stage_types_by_name[name], []).append(name)

        def get_stage_cfg(stage_name: str) -> Dict[str, Any]:
            raw_event_configs = (task.context or {}).get("event_configs") or {}
            stage_type = stage_types_by_name.get(stage_name, normalize_stage_type(stage_name))
            cfg = dict(raw_event_configs.get(stage_type, {})) if stage_type != stage_name else {}
            cfg.update(raw_event_configs.get(stage_name, {}))
            cfg["stage_type"] = stage_type
            return cfg

        def resolve_related_stage(anchor_stage: str, target_type: str, prefer_prior: bool = True) -> str | None:
            normalized = normalize_stage_type(target_type)
            candidates = stages_by_type.get(normalized, [])
            if not candidates:
                return None
            if anchor_stage not in ordered_stage_names:
                return candidates[0]
            anchor_index = ordered_stage_names.index(anchor_stage)
            indexed = [(ordered_stage_names.index(name), name) for name in candidates]
            if prefer_prior:
                prior = [name for idx, name in indexed if idx < anchor_index]
                if prior:
                    return prior[-1]
            if anchor_stage in candidates:
                return anchor_stage
            later = [name for idx, name in indexed if idx >= anchor_index]
            if later:
                return later[0]
            return candidates[-1]

        def create_agent(stage_name: str):
            stage_def = stage_defs_by_name.get(stage_name, {"name": stage_name, "stage_type": normalize_stage_type(stage_name)})
            stage_type = stage_types_by_name.get(stage_name, normalize_stage_type(stage_def.get("stage_type") or stage_name))
            model_adapter = self._select_model(task, stage_name=stage_name, capabilities=stage_caps.get(stage_name, []))
            stage_cfg = get_stage_cfg(stage_name)
            prompt_template = stage_cfg.get("prompt_template")
            progress_callback = None
            if stage_logger:
                progress_callback = lambda payload: stage_logger(stage_name, "progress", {
                    "label": stage_labels.get(stage_name, stage_name),
                    "stage_type": stage_type,
                    **(payload or {}),
                })
            if stage_type == "requirements":
                return ReqAgent(model_adapter, stage_name=stage_name, stage_type=stage_type, prompt_template=prompt_template, progress_callback=progress_callback)
            if stage_type == "architecture":
                return ArchAgent(model_adapter, stage_name=stage_name, stage_type=stage_type, prompt_template=prompt_template, progress_callback=progress_callback)
            if stage_type == "coding":
                return PatchAgent(model_adapter=model_adapter, stage_name=stage_name, stage_type=stage_type, progress_callback=progress_callback)
            if stage_type == "testing":
                return TestAgent(stage_name=stage_name, stage_type=stage_type, progress_callback=progress_callback)
            if stage_type == "docs":
                return DocAgent(model_adapter, stage_name=stage_name, stage_type=stage_type, prompt_template=prompt_template, progress_callback=progress_callback)
            raise ValueError(f"unknown stage type: {stage_type}")

        def make_node(stage_name: str, human_checkpoint: bool):
            current_stage_type = stage_types_by_name.get(stage_name, normalize_stage_type(stage_name))

            def node(state: Dict[str, Any]):
                if state.get("error"):
                    return state
                if should_abort and should_abort(state["task"]):
                    state["abort"] = {"stage": stage_name, "stage_type": current_stage_type, "reason": "task_aborted"}
                    if stage_logger:
                        stage_logger(stage_name, "abort", {"label": stage_labels.get(stage_name, stage_name), "stage_type": current_stage_type, "reason": "task_aborted"})
                    return state
                if human_checkpoint and not state.get("resume", False):
                    state["await"] = {"stage": stage_name, "stage_type": current_stage_type, "label": stage_labels.get(stage_name, stage_name)}
                    if stage_logger:
                        stage_logger(stage_name, "await", {"label": stage_labels.get(stage_name, stage_name), "stage_type": current_stage_type})
                    return state
                task_obj: Task = state["task"]
                collaboration = CollaborationHub(task_obj)

                def default_actor_id(target_stage: str, target_type: str) -> str:
                    mapping = {
                        "requirements": "req-analyst",
                        "architecture": "architect",
                        "coding": "patcher",
                        "testing": "tester",
                        "docs": "doc-writer",
                    }
                    return mapping.get(target_type, target_stage)

                def stage_role_name(target_stage: str, target_type: str, fallback: str = "") -> str:
                    stage_cfg = get_stage_cfg(target_stage)
                    stage_def = stage_defs_by_name.get(target_stage, {})
                    return str(stage_cfg.get("planned_role") or stage_def.get("role") or fallback or target_type)

                def apply_runtime_collaboration_context(target_stage: str) -> None:
                    task_obj.context["_runtime_collaboration"] = {
                        "stage_name": target_stage,
                        "prompt_context": collaboration.build_stage_prompt_context(target_stage),
                        "selection_context": collaboration.build_stage_targeted_context(target_stage),
                    }

                def clear_runtime_collaboration_context() -> None:
                    task_obj.context.pop("_runtime_collaboration", None)

                def record_stage_submission(
                    target_stage: str,
                    target_type: str,
                    payload: Dict[str, Any],
                    *,
                    actor_id: str,
                    actor_role: str,
                ) -> str:
                    conversation_id = collaboration.ensure_thread(
                        target_stage,
                        stage_type=target_type,
                        thread_kind="stage_loop",
                        title=f"{stage_labels.get(target_stage, target_stage)} 协作线程",
                        participants=[
                            {"actor_id": actor_id, "role": actor_role},
                            {"actor_id": f"{target_stage}-reviewer", "role": "阶段评审"},
                        ],
                    )
                    submission = collaboration.post_message(
                        stage_name=target_stage,
                        stage_type=target_type,
                        actor_id=actor_id,
                        actor_role=actor_role,
                        content=CollaborationHub.summarize_submission(payload),
                        message_type="submission",
                        conversation_id=conversation_id,
                        recipient_id=f"{target_stage}-reviewer",
                        payload={"output_summary": payload.get("output_summary") or {}},
                    )
                    collaboration.upsert_blackboard(
                        entry_key=f"stage:{target_stage}:delivery",
                        title=f"{stage_labels.get(target_stage, target_stage)} 最新交付",
                        content=CollaborationHub.summarize_submission(payload),
                        entry_type="stage_delivery",
                        stage_name=target_stage,
                        payload={"output_summary": payload.get("output_summary") or {}},
                        source_message_id=submission.get("message_id"),
                    )
                    review = payload.get("review") or {}
                    if isinstance(review, dict) and review.get("review_status") != "skipped":
                        review_message = collaboration.post_message(
                            stage_name=target_stage,
                            stage_type=target_type,
                            actor_id=f"{target_stage}-reviewer",
                            actor_role="阶段评审",
                            content=CollaborationHub.summarize_review(review),
                            message_type="review_feedback",
                            conversation_id=conversation_id,
                            recipient_id=actor_id,
                            reply_to=submission.get("message_id"),
                            payload=review,
                        )
                        collaboration.upsert_blackboard(
                            entry_key=f"stage:{target_stage}:review",
                            title=f"{stage_labels.get(target_stage, target_stage)} 最新评审",
                            content=CollaborationHub.summarize_review(review),
                            entry_type="stage_review",
                            stage_name=target_stage,
                            payload=review,
                            source_message_id=review_message.get("message_id"),
                        )
                        decision_payload = {
                            "pass": review.get("pass") is True,
                            "stage_type": target_type,
                            "output_summary": payload.get("output_summary") or {},
                            "review_feedback": review.get("feedback"),
                            "next_actions": review.get("next_actions") or [],
                            "risks": review.get("risks") or [],
                        }
                        decision_content = (
                            CollaborationHub.summarize_decision_memory(
                                stage_labels.get(target_stage, target_stage),
                                payload,
                                review,
                            )
                            if review.get("pass") is True
                            else f"{stage_labels.get(target_stage, target_stage)} 评审未通过，原结论暂不固化为长期记忆。"
                        )
                        collaboration.upsert_blackboard(
                            entry_key=f"stage:{target_stage}:decision_memory",
                            title=f"{stage_labels.get(target_stage, target_stage)} 决策记忆",
                            content=decision_content,
                            entry_type="decision_memory",
                            stage_name=target_stage,
                            payload=decision_payload,
                            source_message_id=review_message.get("message_id"),
                        )
                        human_decision = payload.get("human_decision_request") if isinstance(payload.get("human_decision_request"), dict) else None
                        if human_decision:
                            decision_message = collaboration.post_message(
                                stage_name=target_stage,
                                stage_type=target_type,
                                actor_id=f"{target_stage}-reviewer",
                                actor_role="阶段评审",
                                content=(
                                    f"需要人工决策后才能继续推进。\n"
                                    f"问题：{human_decision.get('question')}\n"
                                    + (
                                        "可选方案：" + "；".join([str(item) for item in (human_decision.get("options") or []) if str(item).strip()])
                                        if human_decision.get("options")
                                        else "请直接在对话框中给出你的决定与约束。"
                                    )
                                ).strip(),
                                message_type="review_feedback",
                                conversation_id=conversation_id,
                                recipient_id="user",
                                reply_to=submission.get("message_id"),
                                payload=human_decision,
                            )
                            collaboration.upsert_blackboard(
                                entry_key=f"stage:{target_stage}:human_decision_request",
                                title=f"{stage_labels.get(target_stage, target_stage)} 待人工决策",
                                content=(
                                    f"{human_decision.get('question')}\n"
                                    + (f"原因：{human_decision.get('why_blocked')}" if human_decision.get("why_blocked") else "")
                                ).strip(),
                                entry_type="human_decision_request",
                                stage_name=target_stage,
                                payload={**human_decision, "resolved": False},
                                source_message_id=decision_message.get("message_id"),
                            )
                    return conversation_id

                def post_stage_status(
                    target_stage: str,
                    target_type: str,
                    content: str,
                    *,
                    conversation_id: str | None = None,
                    status_kind: str,
                    status_level: str = "active",
                    actor_id: str = "system",
                    actor_role: str = "流程状态",
                    recipient_id: str | None = None,
                    payload: Dict[str, Any] | None = None,
                ) -> str:
                    resolved_conversation_id = str(conversation_id or collaboration.ensure_thread(
                        target_stage,
                        stage_type=target_type,
                        thread_kind="stage_loop",
                        title=f"{stage_labels.get(target_stage, target_stage)} 协作线程",
                    ))
                    collaboration.post_message(
                        stage_name=target_stage,
                        stage_type=target_type,
                        actor_id=actor_id,
                        actor_role=actor_role,
                        content=content,
                        message_type="system_status",
                        conversation_id=resolved_conversation_id,
                        recipient_id=recipient_id,
                        payload={
                            "status_kind": status_kind,
                            "status_level": status_level,
                            **(payload or {}),
                        },
                    )
                    return resolved_conversation_id

                def to_payload(exec_result: Any) -> Dict[str, Any]:
                    payload: Dict[str, Any] = {}
                    if isinstance(exec_result, AgentMessage):
                        for art in exec_result.artifacts:
                            if art.get("uri") is None and art.get("content"):
                                fname = art.get("filename") or "artifact.txt"
                                path = write_text(self.base_dir, task_obj.task_id, fname, art["content"])
                                art["uri"] = path
                            state.setdefault("artifacts", []).append(art)
                        payload = {
                            "artifacts": exec_result.artifacts,
                            "metadata": exec_result.metadata,
                            "intent": exec_result.intent,
                            "actor": exec_result.actor_id,
                            "output_summary": {
                                "intent": exec_result.intent,
                                "artifact_count": len(exec_result.artifacts),
                                "artifact_types": [a.get("type") for a in exec_result.artifacts[:8]],
                                "artifact_uris": [a.get("uri") for a in exec_result.artifacts[:6]],
                            },
                        }
                        return payload
                    stage_artifacts: List[Dict[str, Any]] = []
                    if exec_result["type"] in {"md", "code"}:
                        path = write_text(self.base_dir, task_obj.task_id, exec_result["filename"], exec_result["content"])
                        stage_artifact = {"uri": path, "type": exec_result["type"]}
                        stage_artifacts.append(stage_artifact)
                        state.setdefault("artifacts", []).append(stage_artifact)
                    content_preview = str(exec_result.get("content", ""))[:700]
                    payload = {
                        "artifacts": stage_artifacts,
                        "output_summary": {
                            "result_type": exec_result.get("type"),
                            "filename": exec_result.get("filename"),
                            "artifact_count": len(stage_artifacts),
                            "artifact_types": [a.get("type") for a in stage_artifacts[:8]],
                            "artifact_uris": [a.get("uri") for a in stage_artifacts[:6]],
                            "content_preview": content_preview,
                            "content_length": len(str(exec_result.get("content", ""))),
                        },
                    }
                    return payload

                def execute_stage_once(target_stage: str, reason: str | None = None) -> Dict[str, Any] | None:
                    target_type = stage_types_by_name.get(target_stage, normalize_stage_type(target_stage))
                    target_cfg = get_stage_cfg(target_stage)
                    actor_id = default_actor_id(target_stage, target_type)
                    actor_role = stage_role_name(target_stage, target_type, fallback=target_type)
                    if target_type == "coding" and reason in {"review_rework", "smoke_fix", "fix_from_testing", "after_architecture_rework"}:
                        orphan_removed = cleanup_architecture_orphan_files(
                            task_obj,
                            self.base_dir,
                            target_stage,
                            {"stage_type": target_type},
                        )
                        if orphan_removed and stage_logger:
                            stage_logger(target_stage, "progress", {
                                "label": stage_labels.get(target_stage, target_stage),
                                "stage_type": target_type,
                                "progress_kind": "cleanup",
                                "progress_state": "done",
                                "reason": reason,
                                "removed_orphans": orphan_removed,
                                "message": f"已清理 {orphan_removed} 个架构清单外的废弃文件",
                            })
                    if should_abort and should_abort(task_obj):
                        state["abort"] = {"stage": target_stage, "stage_type": target_type, "reason": "task_aborted"}
                        if stage_logger:
                            stage_logger(target_stage, "abort", {"label": stage_labels.get(target_stage, target_stage), "stage_type": target_type, "reason": "task_aborted"})
                        return None
                    apply_runtime_collaboration_context(target_stage)
                    conversation_id = None
                    try:
                        agent = create_agent(target_stage)
                        actor_id = getattr(agent, "id", actor_id)
                        actor_role = getattr(agent, "role_name", actor_role) or actor_role
                        conversation_id = post_stage_status(
                            target_stage,
                            target_type,
                            f"{stage_labels.get(target_stage, target_stage)} 已交给 {actor_role}，等待模型返回。",
                            status_kind="agent_waiting",
                            status_level="waiting",
                            actor_id=f"{target_stage}-system",
                            payload={"reason": reason or "", "planned_role": actor_role},
                            recipient_id=actor_id,
                        )
                        if stage_logger:
                            start_payload = {"label": stage_labels.get(target_stage, target_stage), "stage_type": target_type}
                            if reason:
                                start_payload["reason"] = reason
                            stage_logger(target_stage, "start", start_payload)
                        exec_result = agent.act(task_obj, SystemState())
                        conversation_id = post_stage_status(
                            target_stage,
                            target_type,
                            f"{stage_labels.get(target_stage, target_stage)} 已收到模型返回，正在整理产物与上下文。",
                            conversation_id=conversation_id,
                            status_kind="agent_returned",
                            status_level="active",
                            actor_id=f"{target_stage}-system",
                            payload={"planned_role": actor_role},
                        )
                    except Exception as e:
                        clear_runtime_collaboration_context()
                        err_text = str(e)
                        if conversation_id:
                            post_stage_status(
                                target_stage,
                                target_type,
                                f"{stage_labels.get(target_stage, target_stage)} 执行失败：{err_text}",
                                conversation_id=conversation_id,
                                status_kind="agent_error",
                                status_level="error",
                                actor_id=f"{target_stage}-system",
                                payload={"error": err_text},
                            )
                        if target_type == "coding" and err_text.startswith(("architecture_missing_doc:", "architecture_missing_file_list_section:", "architecture_invalid_file_list:")):
                            architecture_stage = resolve_related_stage(target_stage, "architecture", prefer_prior=True)
                            if not architecture_stage:
                                if stage_logger:
                                    stage_logger(target_stage, "error", {"error": err_text, "label": stage_labels.get(target_stage, target_stage), "stage_type": target_type})
                                state["error"] = err_text
                                return None
                            arch_attempts = int(state.setdefault("arch_rework_attempts", {}).get(architecture_stage, 0)) + 1
                            state.setdefault("arch_rework_attempts", {})[architecture_stage] = arch_attempts
                            if stage_logger:
                                stage_logger(architecture_stage, "rework", {
                                    "label": stage_labels.get(architecture_stage, architecture_stage),
                                    "stage_type": stage_types_by_name.get(architecture_stage, "architecture"),
                                    "attempt": arch_attempts,
                                    "reason": "coding_prerequisite_missing",
                                    "feedback": err_text,
                                })
                            architecture_conversation = collaboration.ensure_thread(
                                architecture_stage,
                                stage_type="architecture",
                                thread_kind="prerequisite_rework",
                                peer_stage=target_stage,
                                title=f"{stage_labels.get(architecture_stage, architecture_stage)} 前置条件修复",
                                participants=[
                                    {"actor_id": actor_id, "role": actor_role},
                                    {"actor_id": "architect", "role": stage_role_name(architecture_stage, "architecture", "架构设计师")},
                                ],
                            )
                            feedback_message = collaboration.post_message(
                                stage_name=architecture_stage,
                                stage_type="architecture",
                                actor_id=actor_id,
                                actor_role=actor_role,
                                content=(
                                    "编码阶段发现架构前置条件缺失，需要返工架构文件清单与模块职责。\n"
                                    f"失败信息：{err_text}"
                                ),
                                message_type="prerequisite_feedback",
                                conversation_id=architecture_conversation,
                                thread_kind="prerequisite_rework",
                                recipient_id="architect",
                                payload={"source_stage": target_stage, "error": err_text},
                            )
                            collaboration.upsert_blackboard(
                                entry_key=f"stage:{architecture_stage}:prerequisite_gap",
                                title=f"{stage_labels.get(architecture_stage, architecture_stage)} 前置缺口",
                                content=f"{stage_labels.get(target_stage, target_stage)} 反馈：{err_text}",
                                entry_type="prerequisite_gap",
                                stage_name=architecture_stage,
                                payload={"source_stage": target_stage, "error": err_text},
                                source_message_id=feedback_message.get("message_id"),
                            )
                            post_stage_status(
                                architecture_stage,
                                "architecture",
                                f"{stage_labels.get(target_stage, target_stage)} 发现前置条件缺口，已转交架构阶段返工。",
                                conversation_id=architecture_conversation,
                                status_kind="prerequisite_rework",
                                status_level="warning",
                                actor_id=f"{architecture_stage}-system",
                                payload={"source_stage": target_stage, "error": err_text},
                            )
                            return execute_stage_once(architecture_stage, reason=f"{target_stage}_prerequisite_rework")
                        if stage_logger:
                            stage_logger(target_stage, "error", {"error": err_text, "label": stage_labels.get(target_stage, target_stage), "stage_type": target_type})
                        state["error"] = err_text
                        return None
                    finally:
                        clear_runtime_collaboration_context()

                    payload = to_payload(exec_result)
                    payload["stage"] = target_stage
                    payload["stage_type"] = target_type
                    payload["label"] = stage_labels.get(target_stage, target_stage)
                    if target_cfg.get("planned_role"):
                        payload["planned_role"] = target_cfg.get("planned_role")
                    if target_cfg.get("acceptance_criteria"):
                        payload["acceptance_criteria"] = target_cfg.get("acceptance_criteria")
                    review_progress = None
                    if stage_logger:
                        review_progress = lambda review_payload: stage_logger(target_stage, "progress", {
                            "label": stage_labels.get(target_stage, target_stage),
                            "stage_type": target_type,
                            **(review_payload or {}),
                        })
                    conversation_id = post_stage_status(
                        target_stage,
                        target_type,
                        f"{stage_labels.get(target_stage, target_stage)} 产物已生成，正在发起阶段评审。",
                        conversation_id=conversation_id,
                        status_kind="review_started",
                        status_level="active",
                        actor_id=f"{target_stage}-system",
                    )
                    payload["review"] = self._review_stage_output(task_obj, target_stage, payload, stage_type=target_type, progress_callback=review_progress)
                    payload["human_decision_request"] = _extract_human_decision_request(
                        target_stage,
                        target_type,
                        stage_labels.get(target_stage, target_stage),
                        payload.get("review") or {},
                    )
                    review = payload.get("review") or {}
                    review_text = "评审已完成。"
                    review_level = "done"
                    if isinstance(review, dict) and review.get("review_status") == "skipped":
                        review_text = f"{stage_labels.get(target_stage, target_stage)} 跳过了阶段评审。"
                        review_level = "active"
                    elif isinstance(review, dict) and review.get("pass") is True:
                        review_text = f"{stage_labels.get(target_stage, target_stage)} 评审通过，准备进入下一步。"
                        review_level = "done"
                    elif isinstance(review, dict) and review.get("pass") is False:
                        review_text = f"{stage_labels.get(target_stage, target_stage)} 评审未通过，等待返工处理。"
                        review_level = "warning"
                    post_stage_status(
                        target_stage,
                        target_type,
                        review_text,
                        conversation_id=conversation_id,
                        status_kind="review_finished",
                        status_level=review_level,
                        actor_id=f"{target_stage}-system",
                        payload={"review": review},
                    )
                    if stage_logger:
                        stage_logger(target_stage, "done", payload)
                        stage_logger(target_stage, "review", {
                            "label": stage_labels.get(target_stage, target_stage),
                            "stage_type": target_type,
                            **(payload.get("review") or {}),
                        })
                    payload["conversation_id"] = record_stage_submission(
                        target_stage,
                        target_type,
                        payload,
                        actor_id=actor_id,
                        actor_role=actor_role,
                    )
                    return payload

                def handle_stage_review_rework(target_stage: str, current_payload: Dict[str, Any]) -> Dict[str, Any] | None:
                    target_type = stage_types_by_name.get(target_stage, normalize_stage_type(target_stage))
                    target_cfg = get_stage_cfg(target_stage)
                    rework_limit = int(target_cfg.get("auto_rework_limit", 1) or 1)
                    review_blocking = _is_review_blocking(target_type, target_cfg)
                    review_rework_enabled = review_blocking or target_type in {"coding", "testing"}
                    rework_cleanup = bool(target_cfg.get("rework_cleanup", False))
                    rework_attempts = int(state.setdefault("rework_attempts", {}).get(target_stage, 0))
                    review = current_payload.get("review") or {}
                    if review.get("pass") is False and review_rework_enabled and rework_attempts < rework_limit:
                        if stage_logger:
                            stage_logger(target_stage, "rework", {
                                "label": stage_labels.get(target_stage, target_stage),
                                "stage_type": target_type,
                                "attempt": rework_attempts + 1,
                                "feedback": review.get("feedback", ""),
                                "cleanup": rework_cleanup,
                            })
                        if rework_cleanup:
                            self._cleanup_artifacts(task_obj, current_payload.get("artifacts") or [])
                            if current_payload.get("artifacts"):
                                keep = {str(a.get("uri")) for a in (current_payload.get("artifacts") or [])}
                                state["artifacts"] = [a for a in state.get("artifacts", []) if str(a.get("uri")) not in keep]
                        feedback = str(review.get("feedback") or "")
                        rework_guidance = _build_rework_guidance(target_type, feedback, attempt=rework_attempts)
                        conversation_id = str(current_payload.get("conversation_id") or collaboration.ensure_thread(
                            target_stage,
                            stage_type=target_type,
                            thread_kind="stage_loop",
                            title=f"{stage_labels.get(target_stage, target_stage)} 协作线程",
                        ))
                        reviewer_feedback = collaboration.post_message(
                            stage_name=target_stage,
                            stage_type=target_type,
                            actor_id=f"{target_stage}-reviewer",
                            actor_role="阶段评审",
                            content=(
                                f"请根据以下评审意见继续返工：{feedback}\n"
                                + ("\n要求：基于现有文件做最小增量修复。" if target_type == "coding" and not rework_cleanup else "\n要求：严格修复后重新提交完整产物。")
                                + (f"\n补充约束：\n{rework_guidance}" if rework_guidance else "")
                            ).strip(),
                            message_type="rework_request",
                            conversation_id=conversation_id,
                            recipient_id=default_actor_id(target_stage, target_type),
                            payload={**review, "rework_guidance": rework_guidance},
                        )
                        post_stage_status(
                            target_stage,
                            target_type,
                            f"{stage_labels.get(target_stage, target_stage)} 已根据评审意见进入返工。",
                            conversation_id=conversation_id,
                            status_kind="review_rework",
                            status_level="warning",
                            actor_id=f"{target_stage}-system",
                            payload={"feedback": feedback, "attempt": rework_attempts + 1},
                        )
                        collaboration.upsert_blackboard(
                            entry_key=f"stage:{target_stage}:active_rework",
                            title=f"{stage_labels.get(target_stage, target_stage)} 当前返工要求",
                            content=((feedback + ("\n\n" + rework_guidance if rework_guidance else "")).strip() or "评审未通过，需要继续修复。"),
                            entry_type="rework_request",
                            stage_name=target_stage,
                            payload={**review, "rework_guidance": rework_guidance},
                            source_message_id=reviewer_feedback.get("message_id"),
                        )
                        state.setdefault("rework_attempts", {})[target_stage] = rework_attempts + 1
                        current_payload = execute_stage_once(target_stage, reason="review_rework")
                        if current_payload is None:
                            return None
                        review = current_payload.get("review") or {}
                    if review.get("pass") is False and review_blocking:
                        err = f"stage_review_failed:{target_stage}"
                        if stage_logger:
                            stage_logger(target_stage, "error", {
                                "label": stage_labels.get(target_stage, target_stage),
                                "stage_type": target_type,
                                "error": err,
                                "feedback": review.get("feedback", ""),
                            })
                        state["error"] = err
                        return None
                    return current_payload

                def handle_coding_smoke_loop(current_payload: Dict[str, Any], coding_stage: str, reason_prefix: str = "coding") -> Dict[str, Any] | None:
                    coding_cfg = get_stage_cfg(coding_stage)
                    smoke_fix_limit = int(coding_cfg.get("auto_smoke_fix_limit", 2) or 2)
                    smoke_blocking = bool(coding_cfg.get("smoke_test_blocking", True))
                    smoke_attempts = int(state.setdefault("smoke_fix_attempts", {}).get(coding_stage, 0))
                    while self._smoke_test_failed(current_payload) and smoke_attempts < smoke_fix_limit:
                        smoke_attempts += 1
                        state.setdefault("smoke_fix_attempts", {})[coding_stage] = smoke_attempts
                        smoke_feedback = self._collect_smoke_feedback(current_payload)
                        if stage_logger:
                            stage_logger(coding_stage, "rework", {
                                "label": stage_labels.get(coding_stage, coding_stage),
                                "stage_type": stage_types_by_name.get(coding_stage, "coding"),
                                "attempt": smoke_attempts,
                                "reason": f"{reason_prefix}_smoke_failed",
                                "feedback": smoke_feedback,
                            })
                        conversation_id = str(current_payload.get("conversation_id") or collaboration.ensure_thread(
                            coding_stage,
                            stage_type="coding",
                            thread_kind="stage_loop",
                            title=f"{stage_labels.get(coding_stage, coding_stage)} 协作线程",
                        ))
                        smoke_guidance = _build_rework_guidance("coding", smoke_feedback, attempt=smoke_attempts - 1)
                        smoke_message = collaboration.post_message(
                            stage_name=coding_stage,
                            stage_type="coding",
                            actor_id=f"{coding_stage}-smoke",
                            actor_role="编码冒烟测试",
                            content=(
                                "轻量运行/冒烟校验未通过，请优先做最小增量修复。\n"
                                f"{smoke_feedback}"
                                + (f"\n补充约束：\n{smoke_guidance}" if smoke_guidance else "")
                            ).strip(),
                            message_type="smoke_feedback",
                            conversation_id=conversation_id,
                            recipient_id=default_actor_id(coding_stage, "coding"),
                            payload={"reason_prefix": reason_prefix, "feedback": smoke_feedback, "rework_guidance": smoke_guidance},
                        )
                        post_stage_status(
                            coding_stage,
                            "coding",
                            f"{stage_labels.get(coding_stage, coding_stage)} 冒烟未通过，正在进行自动修复。",
                            conversation_id=conversation_id,
                            status_kind="smoke_rework",
                            status_level="warning",
                            actor_id=f"{coding_stage}-system",
                            payload={"feedback": smoke_feedback, "attempt": smoke_attempts},
                        )
                        collaboration.upsert_blackboard(
                            entry_key=f"stage:{coding_stage}:smoke_feedback",
                            title=f"{stage_labels.get(coding_stage, coding_stage)} 冒烟反馈",
                            content=((smoke_feedback + ("\n\n" + smoke_guidance if smoke_guidance else "")).strip() or "编码阶段冒烟校验失败。"),
                            entry_type="smoke_feedback",
                            stage_name=coding_stage,
                            payload={"reason_prefix": reason_prefix, "feedback": smoke_feedback, "rework_guidance": smoke_guidance},
                            source_message_id=smoke_message.get("message_id"),
                        )
                        current_payload = execute_stage_once(coding_stage, reason="smoke_fix")
                        if current_payload is None:
                            return None
                        current_payload = handle_stage_review_rework(coding_stage, current_payload)
                        if current_payload is None:
                            return None
                    if self._smoke_test_failed(current_payload) and smoke_blocking:
                        err = "coding_smoke_failed"
                        if stage_logger:
                            stage_logger(coding_stage, "error", {
                                "label": stage_labels.get(coding_stage, coding_stage),
                                "stage_type": stage_types_by_name.get(coding_stage, "coding"),
                                "error": err,
                                "feedback": self._collect_smoke_feedback(current_payload),
                            })
                        state["error"] = err
                        return None
                    return current_payload

                payload = execute_stage_once(stage_name)
                if payload is None:
                    return state
                actual_stage = str(payload.get("stage") or stage_name)
                actual_stage_type = stage_types_by_name.get(actual_stage, normalize_stage_type(payload.get("stage_type") or actual_stage))
                human_decision = payload.get("human_decision_request") if isinstance(payload.get("human_decision_request"), dict) else None
                if human_decision:
                    task_obj.context["pending_human_decision"] = dict(human_decision)
                    state["await"] = dict(human_decision)
                    if stage_logger:
                        stage_logger(actual_stage, "await", dict(human_decision))
                    return state
                payload = handle_stage_review_rework(actual_stage, payload)
                if payload is None:
                    return state

                if current_stage_type == "coding" and actual_stage != stage_name:
                    payload = execute_stage_once(stage_name, reason="after_architecture_rework")
                    if payload is None:
                        return state
                    actual_stage = stage_name
                    actual_stage_type = current_stage_type
                    payload = handle_stage_review_rework(actual_stage, payload)
                    if payload is None:
                        return state

                if actual_stage_type == "coding":
                    payload = handle_coding_smoke_loop(payload, actual_stage)
                    if payload is None:
                        return state

                if current_stage_type == "testing":
                    testing_cfg = get_stage_cfg(stage_name)
                    test_fix_limit = int(testing_cfg.get("auto_fix_limit", 3) or 3)
                    test_fix_attempts = int(state.setdefault("test_fix_attempts", {}).get(stage_name, 0))
                    while self._testing_failed(payload) and test_fix_attempts < test_fix_limit:
                        test_fix_attempts += 1
                        state.setdefault("test_fix_attempts", {})[stage_name] = test_fix_attempts
                        test_feedback = self._collect_test_feedback(payload)
                        if stage_logger:
                            stage_logger(stage_name, "rework", {
                                "label": stage_labels.get(stage_name, stage_name),
                                "stage_type": current_stage_type,
                                "attempt": test_fix_attempts,
                                "reason": "testing_failed",
                                "feedback": test_feedback,
                            })

                        coding_stage = resolve_related_stage(stage_name, "coding", prefer_prior=True)
                        if not coding_stage:
                            state["error"] = "testing_failed_without_coding_stage"
                            return state
                        handoff_conversation = collaboration.ensure_thread(
                            coding_stage,
                            stage_type="coding",
                            thread_kind="testing_handoff",
                            peer_stage=stage_name,
                            title=f"{stage_labels.get(stage_name, stage_name)} -> {stage_labels.get(coding_stage, coding_stage)} 缺陷回传",
                            participants=[
                                {"actor_id": "tester", "role": stage_role_name(stage_name, "testing", "测试工程师")},
                                {"actor_id": default_actor_id(coding_stage, "coding"), "role": stage_role_name(coding_stage, "coding", "软件工程师")},
                            ],
                        )
                        testing_feedback_message = collaboration.post_message(
                            stage_name=coding_stage,
                            stage_type="coding",
                            actor_id="tester",
                            actor_role=stage_role_name(stage_name, "testing", "测试工程师"),
                            content=(
                                "全面测试阶段发现缺陷，请先修复后再回到全面测试。\n"
                                f"{test_feedback}"
                            ).strip(),
                            message_type="test_feedback",
                            conversation_id=handoff_conversation,
                            thread_kind="testing_handoff",
                            recipient_id=default_actor_id(coding_stage, "coding"),
                            payload={"source_stage": stage_name, "feedback": test_feedback},
                        )
                        collaboration.upsert_blackboard(
                            entry_key=f"stage:{coding_stage}:test_feedback",
                            title=f"{stage_labels.get(coding_stage, coding_stage)} 最新测试反馈",
                            content=test_feedback or "全面测试阶段发现缺陷。",
                            entry_type="test_feedback",
                            stage_name=coding_stage,
                            payload={"source_stage": stage_name, "feedback": test_feedback},
                            source_message_id=testing_feedback_message.get("message_id"),
                        )

                        coding_payload = execute_stage_once(coding_stage, reason="fix_from_testing")
                        if coding_payload is None:
                            return state
                        coding_payload = handle_stage_review_rework(coding_stage, coding_payload)
                        if coding_payload is None:
                            return state
                        coding_payload = handle_coding_smoke_loop(coding_payload, coding_stage, reason_prefix="fix_from_testing")
                        if coding_payload is None:
                            return state

                        payload = execute_stage_once(stage_name, reason="after_code_fix")
                        if payload is None:
                            return state
                        payload = handle_stage_review_rework(stage_name, payload)
                        if payload is None:
                            return state

                    if self._testing_failed(payload):
                        err = "testing_failed_after_rework"
                        if stage_logger:
                            stage_logger(stage_name, "error", {
                                "label": stage_labels.get(stage_name, stage_name),
                                "stage_type": current_stage_type,
                                "error": err,
                                "feedback": self._collect_test_feedback(payload),
                            })
                        state["error"] = err
                        return state
                state.pop("resume", None)
                if not state.get("await"):
                    state.pop("await", None)
                return state

            return node

        first = None
        ordered_names: List[str] = []
        for st in stages:
            name = str(st["name"])
            human_flag = bool(st.get("human_checkpoint", False))
            sg.add_node(name, make_node(name, human_flag))
            ordered_names.append(name)
            if not first:
                first = name

        for idx, name in enumerate(ordered_names):
            next_name = ordered_names[idx + 1] if idx + 1 < len(ordered_names) else END

            def route(state: Dict[str, Any], default_next=next_name):
                if state.get("error") or state.get("await") or state.get("abort"):
                    return END
                return default_next

            sg.add_conditional_edges(name, route, {END: END, next_name: next_name})
        sg.set_entry_point(first)
        return sg.compile()

def init_task_workspace(base_dir: str, task: Task):
    root = os.path.abspath(task.workspace_path or os.path.join(base_dir, task.task_id))
    for sub in ["analysis", "design", "code", "tests", "docs", "logs", "patches", "plan"]:
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root
