"""PatchAgent: generate code files based on spec and architecture plan."""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Optional, List, Callable, Dict, Any

from core import Task, SystemState, new_message
from storage.file_store import FileStore
from plugins.code_execution_plugin import CodeExecutionPlugin
from orchestration.collaboration import append_prompt_with_runtime_context

DEFAULT_CODING_PROMPT = (
    "你是编码工程师。请基于需求与架构文档，为指定文件生成内容。\n"
    "需求：{spec}\n"
    "必须遵循架构文档中的文件路径和职责，不得擅自改动路径。"
)

GENERATED_FILE_EXTENSIONS = {
    "py", "js", "ts", "tsx", "jsx", "json", "yaml", "yml", "toml", "ini",
    "md", "txt", "sh", "css", "scss", "html", "java", "go", "rs", "cpp",
    "c", "h", "hpp", "sql", "xml", "env",
}

GENERATED_STANDALONE_FILENAMES = {
    "Dockerfile",
    "Makefile",
    "README",
    "README.md",
    "requirements.txt",
    "pytest.ini",
    "pyproject.toml",
    ".env",
    ".env.example",
    ".gitignore",
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

MULTI_FILE_PROMPT_MARKERS = (
    "逐文件输出",
    "每个文件用如下格式",
    "按逐文件输出代码",
    "按逐文件代码块给出",
    "### path/to/file",
    "```<language>",
    "<content>",
)
TARGETED_REWORK_MARKERS = (
    "[rework_request]",
    "[review_feedback]",
    "[smoke_feedback]",
    "[test_feedback]",
    "[prerequisite_feedback]",
    "继续返工",
    "最小增量修复",
    "请先修复",
)

PATH_HEADER_RE = re.compile(
    r"^\s*`?(?P<path>(?:\.?/)?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9]+)`?\s*(?:->|=>|：|:)?\s*$"
)
PATH_LABEL_RE = re.compile(
    r"^\s*(?:目标文件路径|文件路径)\s*[：:]\s*`?(?P<path>(?:\.?/)?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9]+)`?\s*(?:->|=>)?\s*$"
)
INLINE_PATH_HEADER_RE = re.compile(
    r"^\s*`?(?P<path>(?:\.?/)?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9]+)`?\s*(?:->|=>|：|:)\s*(?P<content>.*)$"
)
INLINE_PATH_LABEL_RE = re.compile(
    r"^\s*(?:目标文件路径|文件路径)\s*[：:]\s*`?(?P<path>(?:\.?/)?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9]+)`?\s*(?:->|=>|：|:)\s*(?P<content>.*)$"
)
MARKDOWN_SECTION_RE = re.compile(r"^\s{0,3}#{2,6}\s+(?P<body>.+?)\s*$")
BRACE_PATH_RE = re.compile(
    r"(?P<prefix>(?:\.?/)?(?:[\w.\-]+/)+)\{(?P<body>[\w.\-,\s]+)\}(?P<suffix>\.[A-Za-z0-9]+)"
)
PATH_TOKEN_RE = re.compile(r"(?P<path>(?:\.?/)?(?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z0-9]+)")
MODULE_TOKEN_RE = re.compile(r"(?P<module>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)")
SURGICAL_EDIT_BLOCK_RE = re.compile(
    r"<<<<<\s*SEARCH\s*\n(?P<search>[\s\S]*?)\n={5,}\s*\n(?P<replace>[\s\S]*?)\n>>>>>\s*REPLACE",
    re.MULTILINE,
)
TEST_EDIT_REQUEST_MARKERS = (
    "新增测试",
    "补充测试",
    "更新测试",
    "修改测试",
    "update test",
    "modify test",
    "add test",
    "test case",
)

SOURCE_TEXT_ASCII_REPLACEMENTS = {
    "←": "Left",
    "→": "Right",
    "↑": "Up",
    "↓": "Down",
    "â\x86\x90": "Left",
    "â\x86\x92": "Right",
    "â\x86\x91": "Up",
    "â\x86\x93": "Down",
    "Ã¢Â\x86Â\x90": "Left",
    "Ã¢Â\x86Â\x92": "Right",
    "Ã¢Â\x86Â\x91": "Up",
    "Ã¢Â\x86Â\x93": "Down",
    "\u00a0": " ",
}


class PatchAgent:
    id = "patcher"
    role_name = "PatchAgent"
    domain = "software"
    capabilities: List[str] = ["code.edit:v1", "code.diff:v1"]

    def __init__(self, model_adapter=None, storage=None, stage_name: str = "coding", stage_type: str = "coding", progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.model_adapter = model_adapter
        self.storage = storage or FileStore()
        self.executor = CodeExecutionPlugin()
        self.stage_name = stage_name
        self.stage_type = stage_type
        self.progress_callback = progress_callback

    def _stage_config(self, task: Task) -> dict:
        event_configs = (task.context or {}).get("event_configs") or {}
        cfg = dict(event_configs.get(self.stage_type, {})) if self.stage_type != self.stage_name else {}
        cfg.update(event_configs.get(self.stage_name, {}))
        return cfg

    def _extract_architecture_files(self, arch_text: str) -> list[str]:
        lines = arch_text.splitlines()
        in_file_section = False
        candidates: list[str] = []
        path_pattern = re.compile(
            r"(?P<path>(?:\.?/)?(?:[\w\-]+/)*[\w\-.]+\.[A-Za-z0-9]+|(?:\.?/)?(?:[\w\-]+/)+)"
        )
        allowed_ext = {
            "py", "js", "ts", "tsx", "jsx", "json", "yaml", "yml", "toml", "ini",
            "md", "txt", "sh", "css", "scss", "html", "java", "go", "rs", "cpp",
            "c", "h", "hpp", "sql", "xml",
        }

        for raw in lines:
            line = raw.strip()
            if line.startswith("## ") and "文件清单" in line:
                in_file_section = True
                continue
            if in_file_section and line.startswith("## ") and "文件清单" not in line:
                break
            if not in_file_section or not line:
                continue
            m = path_pattern.search(line.replace("`", ""))
            if not m:
                continue
            path = m.group("path").strip().lstrip("./").replace("\\", "/")
            if path:
                candidates.append(path)

        if not candidates:
            return []

        normalized: list[str] = []
        seen = set()
        for path in candidates:
            pp = PurePosixPath(path)
            safe = str(pp)
            if safe.startswith("..") or safe.startswith("/"):
                continue
            ext = safe.rsplit(".", 1)[-1] if "." in safe else ""
            if "/" not in safe and "." not in safe and safe not in {"requirements.txt", "README.md", "pytest.ini", "pyproject.toml"}:
                safe = f"code/{safe}"
            safe = self._normalize_implementation_path(safe)
            if not safe.endswith("/"):
                if ext.lower() not in allowed_ext:
                    continue
            if safe not in seen:
                seen.add(safe)
                normalized.append(safe)
        return normalized

    def _normalize_implementation_path(self, path_text: str) -> str:
        normalized = self._normalize_generated_path(path_text)
        if not normalized:
            return ""
        if normalized.startswith(("code/", "app/")):
            return normalized
        if normalized.startswith(PRESERVED_WORKSPACE_PREFIXES):
            return normalized
        if normalized.startswith(WEB_ROOT_MANAGED_PREFIXES):
            return f"code/{normalized}"
        name = PurePosixPath(normalized).name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if "/" not in normalized and (normalized in WEB_ROOT_MANAGED_FILES or ext in {"html", "css", "js", "jsx", "ts", "tsx"}):
            return f"code/{normalized}"
        return normalized

    def _has_file_list_section(self, arch_text: str) -> bool:
        return "## 文件清单" in arch_text

    def _architecture_runtime_module_hints(self, arch_text: str) -> list[str]:
        modules: list[str] = []
        seen = set()
        for rel_path in self._extract_architecture_files(arch_text):
            normalized = rel_path.replace("\\", "/")
            if not normalized.startswith("code/") or not normalized.endswith(".py"):
                continue
            module_path = normalized[len("code/"):]
            if module_path.endswith("/__init__.py"):
                module_name = module_path[: -len("/__init__.py")].replace("/", ".")
            else:
                module_name = module_path[: -len(".py")].replace("/", ".")
            module_name = module_name.strip(".")
            if not module_name or module_name in seen:
                continue
            seen.add(module_name)
            modules.append(module_name)
        return modules

    def _normalize_generated_path(self, path_text: str) -> str:
        raw = str(path_text or "").strip().strip("`").strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        return str(PurePosixPath(raw)) if raw else ""

    def _is_probable_generated_path(self, candidate: str) -> bool:
        normalized = self._normalize_generated_path(candidate)
        if not normalized or normalized.startswith(("..", "/")):
            return False
        name = PurePosixPath(normalized).name
        if not name:
            return False
        if name in GENERATED_STANDALONE_FILENAMES:
            return True
        if "." not in name:
            return False
        suffix = name.rsplit(".", 1)[-1].lower()
        return suffix in GENERATED_FILE_EXTENSIONS

    def _normalize_prompt_template(self, template: str) -> str:
        lines = []
        for line in str(template or "").splitlines():
            stripped = line.strip()
            if any(marker in stripped for marker in MULTI_FILE_PROMPT_MARKERS):
                continue
            if stripped.startswith("```"):
                continue
            lines.append(line)
        normalized = "\n".join(lines).strip()
        return normalized or DEFAULT_CODING_PROMPT

    def _extract_markdown_section_path(self, line: str) -> Optional[str]:
        stripped = str(line or "").strip()
        if not stripped:
            return None
        match = MARKDOWN_SECTION_RE.match(stripped)
        if not match:
            return None
        return self._extract_path_header_candidate(match.group("body"))

    def _extract_path_header_candidate(self, line: str) -> Optional[str]:
        stripped = str(line or "").strip()
        if not stripped:
            return None
        match = PATH_LABEL_RE.match(stripped) or PATH_HEADER_RE.match(stripped)
        if not match:
            return None
        candidate = self._normalize_generated_path(match.group("path"))
        if not self._is_probable_generated_path(candidate):
            return None
        return candidate or None

    def _extract_path_header_with_content(self, line: str) -> Optional[tuple[str, str]]:
        stripped = str(line or "").strip()
        if not stripped:
            return None

        inline_match = INLINE_PATH_LABEL_RE.match(stripped) or INLINE_PATH_HEADER_RE.match(stripped)
        if inline_match:
            candidate = self._normalize_generated_path(inline_match.group("path"))
            if self._is_probable_generated_path(candidate):
                return candidate, (inline_match.group("content") or "")

        candidate = self._extract_path_header_candidate(stripped)
        if candidate:
            return candidate, ""
        return None

    def _path_matches_target(self, candidate: str, rel_path: str) -> bool:
        normalized_target = self._normalize_generated_path(rel_path)
        if not candidate or not normalized_target:
            return False
        if candidate == normalized_target:
            return True
        return PurePosixPath(candidate).name == PurePosixPath(normalized_target).name

    def _extract_target_file_block(self, rel_path: str, text: str) -> str:
        lines = text.splitlines()
        extracted: list[str] = []
        found_target = False

        for line in lines:
            header = self._extract_path_header_with_content(line)
            if not found_target:
                if not header:
                    continue
                candidate, inline_content = header
                if not self._path_matches_target(candidate, rel_path):
                    continue
                found_target = True
                if inline_content:
                    extracted.append(inline_content)
                continue

            if header:
                candidate, inline_content = header
                if not self._path_matches_target(candidate, rel_path):
                    break
                if inline_content:
                    extracted.append(inline_content)
                continue

            extracted.append(line)

        if not found_target:
            return text
        return "\n".join(extracted).strip()

    def _extract_markdown_target_block(self, rel_path: str, text: str) -> str:
        lines = text.splitlines()
        extracted: list[str] = []
        found_target = False
        in_fence = False
        saw_fence = False

        for line in lines:
            stripped = line.strip()
            section_path = self._extract_markdown_section_path(stripped)
            if not found_target:
                if section_path and self._path_matches_target(section_path, rel_path):
                    found_target = True
                continue

            if section_path and not in_fence:
                break

            if stripped.startswith("```"):
                saw_fence = True
                in_fence = not in_fence
                if not in_fence:
                    break
                continue

            if saw_fence:
                if in_fence:
                    extracted.append(line)
                continue

            extracted.append(line)

        if not found_target:
            return text
        extracted_text = "\n".join(extracted).strip()
        return extracted_text or text

    def _derive_python_package_inits(self, files: list[str]) -> list[str]:
        package_inits: list[str] = []
        seen = set(files)
        for rel_path in files:
            normalized = rel_path.replace("\\", "/")
            if not normalized.endswith(".py"):
                continue
            parent = PurePosixPath(normalized).parent
            while str(parent) not in {"", "."}:
                init_path = f"{parent.as_posix()}/__init__.py"
                if init_path not in seen:
                    seen.add(init_path)
                    package_inits.append(init_path)
                parent = parent.parent
        return package_inits

    def _should_force_minimal_init(self, rel_path: str, event_cfg: Optional[dict] = None) -> bool:
        cfg = event_cfg or {}
        if cfg.get("force_minimal_init_files") is False:
            return False
        normalized = rel_path.replace("\\", "/")
        return normalized.endswith("/__init__.py")

    def _runtime_prompt_context(self, task: Task) -> str:
        runtime = ((task.context or {}).get("_runtime_collaboration") or {}) if isinstance(task.context, dict) else {}
        if runtime.get("stage_name") != self.stage_name:
            return ""
        return str(runtime.get("prompt_context") or "").strip()

    def _runtime_selection_context(self, task: Task) -> str:
        runtime = ((task.context or {}).get("_runtime_collaboration") or {}) if isinstance(task.context, dict) else {}
        if runtime.get("stage_name") != self.stage_name:
            return ""
        selection_text = str(runtime.get("selection_context") or "").strip()
        if selection_text:
            return selection_text
        return str(runtime.get("prompt_context") or "").strip()

    def _emit_progress(self, **payload: Any) -> None:
        if not self.progress_callback:
            return
        try:
            self.progress_callback(payload)
        except Exception:
            return

    def _match_known_file(self, candidate: str, known_files: list[str]) -> Optional[str]:
        for rel_path in known_files:
            if self._path_matches_target(candidate, rel_path):
                return rel_path
        return None

    def _module_aliases_for_file(self, rel_path: str) -> list[str]:
        normalized = self._normalize_generated_path(rel_path)
        if not normalized.endswith(".py"):
            return []
        aliases: list[str] = []
        if normalized.startswith("code/"):
            module_path = normalized[len("code/"):]
            if module_path.endswith("/__init__.py"):
                alias = module_path[: -len("/__init__.py")].replace("/", ".")
            else:
                alias = module_path[: -len(".py")].replace("/", ".")
            alias = alias.strip(".")
            if alias:
                aliases.append(alias)
        elif normalized.startswith("tests/"):
            module_path = normalized[: -len(".py")].replace("/", ".")
            if module_path:
                aliases.append(module_path)
        return aliases

    def _extract_feedback_target_files(self, feedback_text: str, known_files: list[str]) -> list[str]:
        raw = str(feedback_text or "")
        if not raw:
            return []
        normalized_files = [self._normalize_generated_path(path) for path in known_files if self._normalize_generated_path(path)]
        seen = set()
        module_alias_map: dict[str, str] = {}
        for rel_path in normalized_files:
            for alias in self._module_aliases_for_file(rel_path):
                module_alias_map.setdefault(alias, rel_path)

        def add_match(candidate: str):
            match = self._match_known_file(candidate, normalized_files)
            if match:
                seen.add(match)

        compact = raw.replace("`", "")
        for brace_match in BRACE_PATH_RE.finditer(compact):
            prefix = brace_match.group("prefix") or ""
            suffix = brace_match.group("suffix") or ""
            names = [item.strip() for item in str(brace_match.group("body") or "").split(",") if item.strip()]
            for name in names:
                add_match(f"{prefix}{name}{suffix}")

        for token in PATH_TOKEN_RE.finditer(compact):
            add_match(token.group("path"))

        for token in MODULE_TOKEN_RE.finditer(compact):
            module_name = str(token.group("module") or "").strip(".")
            if module_name in module_alias_map:
                seen.add(module_alias_map[module_name])

        basename_map: dict[str, list[str]] = {}
        for rel_path in normalized_files:
            basename_map.setdefault(PurePosixPath(rel_path).name, []).append(rel_path)
        for basename, rel_paths in basename_map.items():
            if len(rel_paths) != 1:
                continue
            if basename and basename in compact:
                add_match(rel_paths[0])
        return [rel_path for rel_path in normalized_files if rel_path in seen]

    def _feedback_requests_test_edits(self, feedback_text: str) -> bool:
        text = str(feedback_text or "").lower()
        return any(marker in text for marker in TEST_EDIT_REQUEST_MARKERS)

    def _prefer_code_targets_for_contract_rework(self, files: list[str], feedback_text: str) -> list[str]:
        text = str(feedback_text or "")
        if not text:
            return files
        lowered = text.lower()
        if not any(marker in text for marker in ("[smoke_feedback]", "[test_feedback]")) and not any(token in lowered for token in ("pytest", "test_", "smoke")):
            return files
        if self._feedback_requests_test_edits(text):
            return files
        non_test_files = [rel_path for rel_path in files if not self._normalize_generated_path(rel_path).startswith("tests/")]
        return non_test_files or files

    def _prefer_targeted_rework_mode(self, files: list[str], feedback_text: str, workspace_root: str) -> bool:
        text = str(feedback_text or "")
        if not text:
            return False
        lowered = text.lower()
        if not any(marker in text for marker in TARGETED_REWORK_MARKERS) and not any(token in lowered for token in ("pytest", "test_", "smoke", "assert", "traceback")):
            return False
        existing_files = [rel_path for rel_path in files if os.path.isfile(os.path.join(workspace_root, rel_path))]
        return bool(existing_files)

    def _select_generation_files(self, files: list[str], task: Task, workspace_root: str, event_cfg: Optional[dict] = None) -> tuple[list[str], dict]:
        cfg = event_cfg or {}
        if cfg.get("targeted_rework_enabled") is False:
            return files, {"mode": "full"}
        feedback_text = self._runtime_selection_context(task)
        if not feedback_text or not any(marker in feedback_text for marker in TARGETED_REWORK_MARKERS):
            return files, {"mode": "full"}
        if not any(os.path.exists(os.path.join(workspace_root, rel_path)) for rel_path in files):
            return files, {"mode": "full"}
        targets = self._extract_feedback_target_files(feedback_text, files)
        if not targets or len(targets) >= len(files):
            preferred = self._prefer_code_targets_for_contract_rework(files, feedback_text)
            if preferred != files or self._prefer_targeted_rework_mode(preferred, feedback_text, workspace_root):
                return preferred, {
                    "mode": "targeted_rework",
                    "target_files": preferred,
                    "feedback_excerpt": feedback_text[:600],
                    "preserve_existing_tests": True,
                }
            return files, {"mode": "full", "feedback_excerpt": feedback_text[:600]}
        target_set = {self._normalize_generated_path(path) for path in targets}
        selected = [rel_path for rel_path in files if self._normalize_generated_path(rel_path) in target_set]
        if not selected:
            return files, {"mode": "full", "feedback_excerpt": feedback_text[:600]}
        selected = self._prefer_code_targets_for_contract_rework(selected, feedback_text)
        return selected, {
            "mode": "targeted_rework",
            "target_files": selected,
            "feedback_excerpt": feedback_text[:600],
        }

    def _collect_workspace_python_files(self, workspace_root: str) -> list[str]:
        collected: list[str] = []
        seen = set()
        for folder in ("code", "tests"):
            base = os.path.join(workspace_root, folder)
            if not os.path.isdir(base):
                continue
            for root, _, file_names in os.walk(base):
                for file_name in sorted(file_names):
                    if not file_name.endswith(".py"):
                        continue
                    abs_path = os.path.join(root, file_name)
                    if abs_path in seen:
                        continue
                    seen.add(abs_path)
                    collected.append(abs_path)
        return collected

    def _default_smoke_commands(self, workspace_root: str, py_files: list[str], test_files: list[str]) -> list[str]:
        commands: list[str] = []
        if py_files:
            rel_py_files = [os.path.relpath(path, workspace_root) for path in py_files]
            commands.append("python -m py_compile " + " ".join(rel_py_files))
        if test_files:
            rel_test_files = [os.path.relpath(path, workspace_root) for path in test_files]
            if len(rel_test_files) <= 12:
                commands.append("pytest -q --maxfail=1 " + " ".join(rel_test_files))
            else:
                commands.append("pytest -q --collect-only")
        return commands

    def _sanitize_generated_content(self, rel_path: str, raw_text: str) -> str:
        text = str(raw_text or "").replace("\r\n", "\n").strip()
        suffix = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""

        text = self._extract_markdown_target_block(rel_path, text)
        text = self._extract_target_file_block(rel_path, text)

        fence_matches = re.findall(r"```(?:[A-Za-z0-9_+\-]+)?\s*\n([\s\S]*?)```", text)
        if fence_matches:
            text = max(fence_matches, key=lambda item: len(item.strip())).strip()

        lines = text.splitlines()
        cleaned_lines: list[str] = []
        started = False
        for line in lines:
            stripped = line.strip()
            if not started:
                if not stripped:
                    continue
                if stripped.startswith(("我先", "下面是", "以下是", "先读取", "先查看", "说明", "修复说明", "输出如下", "Here's", "Below is")):
                    continue
                if stripped.startswith("```"):
                    continue
                header = self._extract_path_header_with_content(stripped)
                if header and self._path_matches_target(header[0], rel_path):
                    if header[1]:
                        cleaned_lines.append(header[1])
                        started = True
                    continue
                started = True
            if stripped.startswith("```"):
                continue
            header = self._extract_path_header_with_content(stripped)
            if header and cleaned_lines and not self._path_matches_target(header[0], rel_path):
                break
            if header and self._path_matches_target(header[0], rel_path):
                if header[1]:
                    cleaned_lines.append(header[1])
                continue
            cleaned_lines.append(line)
        text = "\n".join(cleaned_lines).strip()
        text = self._normalize_source_text(text)

        if suffix == "py":
            text = re.sub(r"\A(?:先读取|我先|下面是|以下是|说明：|修复说明：)[^\n]*\n+", "", text)
        return text + ("\n" if text and not text.endswith("\n") else "")

    def _normalize_source_text(self, text: str) -> str:
        normalized = unicodedata.normalize("NFC", str(text or ""))
        for source, replacement in SOURCE_TEXT_ASCII_REPLACEMENTS.items():
            normalized = normalized.replace(source, replacement)
        return normalized

    def _validate_generated_content(self, rel_path: str, content: str) -> None:
        suffix = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""
        if suffix == "py":
            suspicious_markers = ["```", "我先", "下面是", "以下是", "先读取", "先查看", "修复说明", "apply_patch"]
            if any(marker in content for marker in suspicious_markers):
                raise ValueError(f"generated_python_contains_explanatory_text:{rel_path}")
            if re.search(r"(?m)^\s*(?:from|import)\s+code(?:\.|\s|$)", content):
                raise ValueError(f"generated_python_uses_code_package_import:{rel_path}")
            first_non_empty = next((line.strip() for line in content.splitlines() if line.strip()), "")
            if first_non_empty:
                header = self._extract_path_header_with_content(first_non_empty)
                if header:
                    raise ValueError(f"generated_python_contains_path_header:{rel_path}")
            normalized_rel_path = self._normalize_generated_path(rel_path)
            self_import_aliases = [alias for alias in self._module_aliases_for_file(normalized_rel_path) if alias]
            stem = PurePosixPath(normalized_rel_path).stem
            if stem and stem != "__init__":
                self_import_aliases.append(stem)
            seen_aliases = set()
            for alias in self_import_aliases:
                if alias in seen_aliases:
                    continue
                seen_aliases.add(alias)
                import_pattern = re.compile(
                    rf"(?m)^\s*import\s+{re.escape(alias)}(?:\s+as\s+\w+|\s*(?:#.*)?)$"
                )
                from_pattern = re.compile(
                    rf"(?m)^\s*from\s+{re.escape(alias)}\s+import\b"
                )
                relative_pattern = re.compile(
                    rf"(?m)^\s*from\s+\.+{re.escape(stem)}\s+import\b"
                ) if stem and "." not in alias else None
                if import_pattern.search(content) or from_pattern.search(content) or (relative_pattern and relative_pattern.search(content)):
                    raise ValueError(f"generated_python_self_import:{rel_path}:{alias}")
            try:
                compile(content, rel_path, "exec")
            except SyntaxError as exc:
                raise ValueError(f"generated_python_invalid:{rel_path}:{exc.msg}") from exc

    def _prefer_surgical_edits(self, rel_path: str, task: Task, generation_mode: str, workspace_root: str) -> bool:
        if generation_mode != "targeted_rework":
            return False
        if rel_path.endswith("/__init__.py"):
            return False
        abs_path = os.path.join(workspace_root, rel_path)
        return os.path.isfile(abs_path)

    def _build_surgical_edit_prompt(
        self,
        spec: str,
        rel_path: str,
        existing_content: str,
        arch_text: str,
        prompt_template: Optional[str],
        task: Task,
        extra_feedback: str = "",
    ) -> str:
        template = self._normalize_prompt_template(prompt_template or DEFAULT_CODING_PROMPT)
        try:
            custom_prefix = template.format(spec=spec)
        except Exception:
            custom_prefix = template
        runtime_feedback = self._runtime_selection_context(task)
        prompt = (
            f"{custom_prefix}\n"
            f"目标文件路径：{rel_path}\n"
            "你现在要做的是：基于现有文件做最小必要修改，优先保留健康代码，不要整文件重写。\n"
            "请输出一个或多个精确的 SEARCH/REPLACE 补丁块，格式严格如下：\n"
            "<<<<< SEARCH\n"
            "<原始代码片段，必须与现有文件完全一致>\n"
            "=====\n"
            "<替换后的代码片段>\n"
            ">>>>> REPLACE\n"
            "要求：\n"
            "1) SEARCH 片段必须直接复制自当前文件内容，保持空格缩进完全一致。\n"
            "2) 只修改与当前问题直接相关的局部片段；不要重写无关代码。\n"
            "3) 可输出多个补丁块；除补丁块外不要输出任何解释。\n"
            "4) 如果是 Python 文件，修改后必须保证语法可通过。\n"
            "5) 不要输出 Markdown 代码块围栏。\n"
            "6) 源代码中的注释、提示文案、UI 字符串优先只使用 ASCII；若要表达方向，请使用 Left/Right/Up/Down，不要使用箭头符号或其它特殊字符。\n"
        )
        if extra_feedback:
            prompt += f"7) 必须修复上一轮问题：{extra_feedback}\n"
        if runtime_feedback:
            prompt += f"8) 当前返工上下文：\n{runtime_feedback[:5000]}\n"
        prompt += f"架构文档片段：\n{arch_text[:4000]}\n"
        prompt += f"当前文件内容：\n{existing_content[:12000]}\n"
        return prompt

    def _apply_surgical_edits(self, rel_path: str, original_content: str, patch_text: str) -> str:
        matches = list(SURGICAL_EDIT_BLOCK_RE.finditer(str(patch_text or "")))
        if not matches:
            raise ValueError(f"surgical_edit_invalid_format:{rel_path}")
        updated = str(original_content)
        applied = 0
        for match in matches:
            search = match.group("search")
            replace = match.group("replace")
            if search not in updated:
                raise ValueError(f"surgical_edit_search_not_found:{rel_path}")
            occurrences = updated.count(search)
            if occurrences != 1:
                raise ValueError(f"surgical_edit_search_not_unique:{rel_path}:{occurrences}")
            updated = updated.replace(search, replace, 1)
            applied += 1
        if applied == 0:
            raise ValueError(f"surgical_edit_empty:{rel_path}")
        return updated

    def _build_file_prompt(self, spec: str, rel_path: str, arch_text: str, prompt_template: Optional[str], task: Task, extra_feedback: str = "") -> str:
        template = self._normalize_prompt_template(prompt_template or DEFAULT_CODING_PROMPT)
        try:
            custom_prefix = template.format(spec=spec)
        except Exception:
            custom_prefix = template
        architecture_files = self._extract_architecture_files(arch_text)
        runtime_modules = self._architecture_runtime_module_hints(arch_text)
        python_code_files = [
            path for path in architecture_files
            if path.startswith("code/") and path.endswith(".py") and not path.endswith("/__init__.py")
        ]
        prompt = (
            f"{custom_prefix}\n"
            f"目标文件路径：{rel_path}\n"
            f"架构文档片段：\n{arch_text[:6000]}\n"
            "输出约束：\n"
            "1) 仅输出该文件内容，不要解释。\n"
            "2) 如果是 Python 文件，必须保证语法可通过。\n"
            "3) 不要输出 Markdown 代码块围栏。\n"
            "4) Python 导入时，不要使用 `from code...` 或 `import code...`；`code/` 是源码目录，不是运行时包名。\n"
            "5) 源代码中的注释、提示文案、UI 字符串优先只使用 ASCII；若要表达方向，请使用 Left/Right/Up/Down，不要使用箭头符号或其它特殊字符。\n"
        )
        if extra_feedback:
            prompt += f"6) 必须修复上一轮问题：{extra_feedback}\n"
        prompt += (
            "7) 严禁输出其他文件内容（例如 requirements.txt、别的 .py 文件）。\n"
            "8) 严禁输出“路径 -> 内容”这种多文件拼接格式。\n"
        )
        runtime_feedback = self._runtime_selection_context(task).lower()
        if rel_path.startswith("tests/") and any(token in runtime_feedback for token in ("[smoke_feedback]", "[test_feedback]", "pytest", "test_")):
            prompt += "9) 当前处于失败返工阶段时，除非反馈明确要求修改测试，否则优先保持现有测试契约不变，不要弱化断言。\n"
        if architecture_files:
            prompt += f"10) 必须严格服从文件清单，不要在代码里引用清单外的本地文件/模块。当前文件清单：{', '.join(architecture_files[:30])}。\n"
        if runtime_modules:
            prompt += (
                "11) Python 本地模块导入只能引用这些清单内模块（`code/` 不是包名前缀）："
                f"{', '.join(runtime_modules[:30])}。"
                "如果某模块不在这个列表里，就不要在代码中引用它（例如不存在的 app.*）。\n"
            )
        if rel_path == "code/main.py" and len(python_code_files) > 1:
            prompt += (
                "12) 当前是多模块 Python 项目，`code/main.py` 只负责程序入口、组装和主循环；"
                "不要在这里重复实现已经由其它清单内模块负责的核心数据结构、规则或渲染逻辑。\n"
                "13) 如果 `code/main.py` 需要导入同目录其它模块，请使用可直接脚本运行的兼容写法："
                "优先 `try: from .foo import Bar`，失败时再 `except ImportError: from foo import Bar`；"
                "严禁出现 `from .main import ...`、`import main` 这类自导入。\n"
            )
        return append_prompt_with_runtime_context(prompt, task, self.stage_name)

    def _store_failed_generation(self, task: Task, rel_path: str, raw_text: str, error_text: str, attempt: int) -> None:
        safe_name = rel_path.replace('/', '__')
        content = f"error: {error_text}\n\n{raw_text}"
        self.storage.put(task.task_id, f"patches/{safe_name}.attempt{attempt}.txt", content.encode('utf-8', errors='ignore'))

    def _build_exec_env(self, workspace_root: str) -> dict[str, str]:
        env = dict(os.environ)
        code_root = os.path.join(workspace_root, 'code')
        extra_paths = [workspace_root]
        if os.path.isdir(code_root):
            extra_paths.insert(0, code_root)
        existing = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = os.pathsep.join(extra_paths + ([existing] if existing else []))
        return env

    def _artifact_workspace_path(self, workspace_root: str, uri: str) -> str:
        if os.path.isabs(uri):
            return uri
        if uri.startswith('workspace' + os.sep) or uri.startswith('workspace/'):
            task_root = os.path.dirname(workspace_root)
            return os.path.abspath(os.path.join(task_root, uri.split('/', 1)[1] if '/' in uri else uri))
        return os.path.abspath(os.path.join(workspace_root, uri))

    def _generate_file_content(
        self,
        spec: str,
        rel_path: str,
        arch_text: str,
        prompt_template: Optional[str],
        task: Task,
        *,
        current_index: int = 0,
        total_files: int = 0,
        generation_mode: str = "full",
    ):
        event_cfg = self._stage_config(task)
        max_attempts = int(event_cfg.get("generation_retry_limit", 2) or 2)
        feedback = ""
        last_error = ""
        fallback_used = False
        workspace_root = task.workspace_path or os.path.join("workspace", task.task_id)
        prefer_surgical = self._prefer_surgical_edits(rel_path, task, generation_mode, workspace_root)
        existing_content = ""
        abs_path = os.path.join(workspace_root, rel_path)
        if prefer_surgical:
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    existing_content = f.read()
            except Exception:
                prefer_surgical = False
        for attempt in range(1, max_attempts + 1):
            prompt = (
                self._build_surgical_edit_prompt(spec, rel_path, existing_content, arch_text, prompt_template, task, extra_feedback=feedback)
                if prefer_surgical
                else self._build_file_prompt(spec, rel_path, arch_text, prompt_template, task, extra_feedback=feedback)
            )
            self._emit_progress(
                progress_kind="model",
                progress_state="start",
                generation_mode=generation_mode,
                current_file=rel_path,
                current_index=current_index,
                progress_total=total_files,
                progress_completed=max(0, current_index - 1),
                generation_attempt=attempt,
                generation_attempts=max_attempts,
                message=f"正在请求模型{'片段修补' if prefer_surgical else '生成'} {rel_path}（第 {attempt}/{max_attempts} 次）",
            )
            try:
                code_text = self.model_adapter.generate(prompt, context=task.context)
                code_text_str = str(code_text)
            except Exception as exc:
                last_error = f"coding_model_exception:{rel_path}:{exc}"
                self._store_failed_generation(task, rel_path, "", last_error, attempt)
                self._emit_progress(
                    progress_kind="model",
                    progress_state="retry" if attempt < max_attempts else "error",
                    generation_mode=generation_mode,
                    current_file=rel_path,
                    current_index=current_index,
                    progress_total=total_files,
                    progress_completed=max(0, current_index - 1),
                    generation_attempt=attempt,
                    generation_attempts=max_attempts,
                    message=f"{rel_path} 生成失败，{'准备重试' if attempt < max_attempts else '重试结束'}",
                    error=last_error,
                )
                feedback = f"上一轮生成发生异常：{last_error}。请只输出修复后的完整文件内容。"
                continue
            try:
                if code_text_str.startswith(("[openai-http error]", "[openai error]", "[codex http error]", "[codex error]", "[codex empty response]")):
                    raise ValueError(f"coding_model_failed:{rel_path}:{code_text_str[:240]}")
                if prefer_surgical:
                    sanitized = self._apply_surgical_edits(rel_path, existing_content, code_text_str)
                else:
                    sanitized = self._sanitize_generated_content(rel_path, code_text_str)
                self._validate_generated_content(rel_path, sanitized)
                self._emit_progress(
                    progress_kind="model",
                    progress_state="done",
                    generation_mode=generation_mode,
                    current_file=rel_path,
                    current_index=current_index,
                    progress_total=total_files,
                    progress_completed=max(0, current_index - 1),
                    generation_attempt=attempt,
                    generation_attempts=max_attempts,
                    message=f"模型已返回 {rel_path} 的{'片段修补结果' if prefer_surgical else '可用内容'}",
                )
                return sanitized, fallback_used
            except ValueError as exc:
                last_error = str(exc)
                self._store_failed_generation(task, rel_path, code_text_str, last_error, attempt)
                self._emit_progress(
                    progress_kind="model",
                    progress_state="retry" if attempt < max_attempts else "error",
                    generation_mode=generation_mode,
                    current_file=rel_path,
                    current_index=current_index,
                    progress_total=total_files,
                    progress_completed=max(0, current_index - 1),
                    generation_attempt=attempt,
                    generation_attempts=max_attempts,
                    message=f"{rel_path} {'片段修补' if prefer_surgical else '生成'}结果未通过校验，{'准备重试' if attempt < max_attempts else '重试结束'}",
                    error=last_error,
                )
                if prefer_surgical and last_error.startswith("surgical_edit_"):
                    if last_error.startswith("surgical_edit_search_not_found:") and attempt < max_attempts:
                        prefer_surgical = False
                        fallback_used = True
                        feedback = (
                            f"上一轮局部补丁未能精确定位：{last_error}。"
                            "请改为输出该文件的完整修正版内容，保留健康代码，只修复当前问题，"
                            "并确保所有注释、提示文案、UI 字符串只使用 ASCII 字符。"
                        )
                        continue
                    feedback = (
                        f"上一轮片段修补未通过：{last_error}。"
                        "请重新输出严格的 SEARCH/REPLACE 补丁块，并确保 SEARCH 片段与当前文件完全一致。"
                    )
                elif prefer_surgical:
                    feedback = (
                        f"上一轮片段修补应用后校验失败：{last_error}。"
                        "请继续使用 SEARCH/REPLACE 形式，仅修正必要片段。"
                    )
                else:
                    feedback = f"上一轮生成未通过校验：{last_error}。请只输出修复后的完整文件内容。"
        raise ValueError(last_error or f"coding_generate_failed:{rel_path}")

    def act(self, task: Task, state: SystemState):
        spec = task.context.get("spec", "")
        workspace_root = task.workspace_path or os.path.join("workspace", task.task_id)
        arch_path = os.path.join(workspace_root, "design", "architecture.md")
        files = ["code/main.py"]
        arch_text = ""
        event_cfg = self._stage_config(task)
        prompt_template = event_cfg.get("prompt_template")
        smoke_test_command = (event_cfg.get("smoke_test_command") or "").strip()
        if os.path.exists(arch_path):
            with open(arch_path, "r", encoding="utf-8") as f:
                arch_text = f.read()
            if not self._has_file_list_section(arch_text):
                raise ValueError(
                    "architecture_missing_file_list_section:architecture 文档缺少“## 文件清单”章节，"
                    "请先修正 architecture 阶段输出并重跑。"
                )
            extracted = self._extract_architecture_files(arch_text)
            if extracted:
                files = extracted
            else:
                raise ValueError(
                    "architecture_invalid_file_list:architecture 的“## 文件清单”未提取到有效文件路径，"
                    "请在文件清单中按每行一个相对路径列出待实现文件。"
                )
        else:
            raise ValueError("architecture_missing_doc:缺少 design/architecture.md，请先完成 architecture 阶段。")

        files, selection_meta = self._select_generation_files(files, task, workspace_root, event_cfg)
        package_inits = [
            rel_path for rel_path in self._derive_python_package_inits(files)
            if not os.path.exists(os.path.join(workspace_root, rel_path))
        ]
        if package_inits:
            files = [*package_inits, *files]

        generation_mode = str(selection_meta.get("mode") or "full")
        total_files = len(files)
        self._emit_progress(
            progress_kind="batch",
            progress_state="start",
            generation_mode=generation_mode,
            progress_total=total_files,
            progress_completed=0,
            message=f"准备生成 {total_files} 个文件",
        )

        artifacts = []
        for index, fp in enumerate(files, start=1):
            rel_path = fp.replace("\\", "/")
            self._emit_progress(
                progress_kind="file",
                file_status="start",
                generation_mode=generation_mode,
                current_file=rel_path,
                current_index=index,
                progress_total=total_files,
                progress_completed=max(0, index - 1),
                message=f"正在生成 {rel_path}",
            )
            if rel_path.endswith("/"):
                rel_path = f"{rel_path}.gitkeep"
                uri = self.storage.put(task.task_id, rel_path, b"")
                artifacts.append({"type": "placeholder", "uri": uri, "mime": "text/plain", "fallback": False})
                self._emit_progress(
                    progress_kind="file",
                    file_status="done",
                    generation_mode=generation_mode,
                    current_file=rel_path,
                    current_index=index,
                    progress_total=total_files,
                    progress_completed=index,
                    message=f"已完成 {rel_path}",
                )
                continue
            if self._should_force_minimal_init(rel_path, event_cfg):
                uri = self.storage.put(task.task_id, rel_path, b"")
                artifacts.append({"type": "code", "uri": uri, "mime": "text/plain", "fallback": False})
                self._emit_progress(
                    progress_kind="file",
                    file_status="done",
                    generation_mode=generation_mode,
                    current_file=rel_path,
                    current_index=index,
                    progress_total=total_files,
                    progress_completed=index,
                    message=f"已完成 {rel_path}",
                )
                continue
            content, fallback_used = self._generate_file_content(
                spec,
                rel_path,
                arch_text,
                prompt_template,
                task,
                current_index=index,
                total_files=total_files,
                generation_mode=generation_mode,
            )
            uri = self.storage.put(task.task_id, rel_path, content.encode())
            artifacts.append({"type": "code", "uri": uri, "mime": "text/plain", "fallback": fallback_used})
            self._emit_progress(
                progress_kind="file",
                file_status="done",
                generation_mode=generation_mode,
                current_file=rel_path,
                current_index=index,
                progress_total=total_files,
                progress_completed=index,
                message=f"已完成 {rel_path}",
            )

        py_files = self._collect_workspace_python_files(workspace_root)
        test_files = [path for path in py_files if os.path.basename(path).startswith('test_')]
        exec_env = self._build_exec_env(workspace_root)
        if smoke_test_command:
            smoke_commands = [smoke_test_command]
        else:
            smoke_commands = self._default_smoke_commands(workspace_root, py_files, test_files)

        for smoke_cmd in smoke_commands:
            self._emit_progress(
                progress_kind="smoke",
                progress_state="start",
                generation_mode=generation_mode,
                progress_total=total_files,
                progress_completed=total_files,
                message=f"开始冒烟校验：{smoke_cmd}",
            )
            smoke_result = self.executor.run(smoke_cmd, cwd=workspace_root, env=exec_env)
            exit_code = int(smoke_result.get('exit_code', 0) or 0) if isinstance(smoke_result, dict) else 1
            self._emit_progress(
                progress_kind="smoke",
                progress_state="done",
                generation_mode=generation_mode,
                progress_total=total_files,
                progress_completed=total_files,
                message=f"冒烟校验完成（exit={exit_code}）",
            )
            artifacts.append({
                "type": "smoke_test_result",
                "uri": "inline",
                "content": {"command": smoke_cmd, **(smoke_result or {})},
            })
            if exit_code != 0:
                break

        notes_lines = [
            f"generation_mode: {selection_meta.get('mode', 'full')}",
            f"generated_files: {', '.join(files)}",
        ]
        target_files = selection_meta.get("target_files") or []
        if target_files:
            notes_lines.append(f"target_files: {', '.join(target_files)}")
        feedback_excerpt = str(selection_meta.get("feedback_excerpt") or "").strip()
        if feedback_excerpt:
            notes_lines.append(f"feedback_excerpt: {feedback_excerpt}")
        notes = "\n".join(notes_lines) + "\n"
        self.storage.put(task.task_id, os.path.join("logs", "patch_notes.txt"), notes.encode())

        return new_message(
            self.id,
            task,
            intent="write_code",
            capabilities_used=self.capabilities,
            artifacts=artifacts,
            metadata={"model": getattr(self.model_adapter, "model_name", "unknown")},
        )


__all__ = ["PatchAgent"]
