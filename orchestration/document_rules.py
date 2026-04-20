from __future__ import annotations

import os
import re
from pathlib import PurePosixPath


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
    _lines, sections = _find_architecture_file_sections(arch_text)
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


__all__ = [
    "_architecture_validation_issues",
    "_docs_validation_issues",
    "_extract_architecture_file_list",
    "_extract_file_paths_from_lines",
    "_infer_declared_stack",
    "_infer_project_stack",
    "_normalize_architecture_file_list",
    "_normalize_architecture_markdown",
]
