from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from core import SystemState, Task
from orchestration.collab.context import append_prompt_with_runtime_context
from orchestration.planning.document_rules import (
    _infer_declared_stack,
    _infer_project_stack,
    _normalize_architecture_markdown,
)
from orchestration.planning.stage_catalog import (
    ARCHITECTURE_FILE_LIST_HINT,
    TEXT_OUTPUT_QUALITY_GUARDRAIL,
    normalize_stage_type,
    render_stage_prompt,
)


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


def _load_task_artifact_text(task: Task, rel_path: str) -> str:
    workspace = os.path.abspath(task.workspace_path or "")
    if not workspace:
        return ""
    abs_path = os.path.join(workspace, rel_path)
    if not os.path.exists(abs_path):
        return ""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except Exception:
        return ""


class _BaseGeneratedDocAgent:
    id = ""
    capabilities: List[str] = []
    default_action_label = "正在生成文档"
    failure_prefix = "document_model_failed"

    def __init__(
        self,
        model_adapter,
        stage_name: str,
        stage_type: str,
        prompt_template: str | None = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.model_adapter = model_adapter
        self.stage_name = stage_name
        self.stage_type = normalize_stage_type(stage_type)
        self.prompt_template = prompt_template
        self.progress_callback = progress_callback

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

    def _base_prompt(self, task: Task) -> str:
        prompt = render_stage_prompt(self.stage_name, task.context.get("spec", ""), self.prompt_template, stage_type=self.stage_type)
        prompt = (prompt.rstrip() + TEXT_OUTPUT_QUALITY_GUARDRAIL).strip()
        return append_prompt_with_runtime_context(prompt, task, self.stage_name)


class ReqAgent(_BaseGeneratedDocAgent):
    id = "req-analyst"
    capabilities = []
    default_action_label = "正在生成需求文档"
    failure_prefix = "requirements_model_failed"

    def __init__(self, model_adapter, stage_name: str = "requirements", stage_type: str = "requirements", prompt_template: str | None = None, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        super().__init__(model_adapter, stage_name=stage_name, stage_type=stage_type, prompt_template=prompt_template, progress_callback=progress_callback)

    def act(self, task: Task, state: SystemState):
        text = self._generate_text(task, self._base_prompt(task), self.default_action_label)
        failure = _model_failure_text(text)
        if failure:
            raise ValueError(f"{self.failure_prefix}:{failure[:240]}")
        return {"type": "md", "filename": "analysis/requirements.md", "content": text}


class ArchAgent(_BaseGeneratedDocAgent):
    id = "architect"
    capabilities = []
    default_action_label = "正在生成架构方案"
    failure_prefix = "architecture_model_failed"

    def __init__(self, model_adapter, stage_name: str = "architecture", stage_type: str = "architecture", prompt_template: str | None = None, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        super().__init__(model_adapter, stage_name=stage_name, stage_type=stage_type, prompt_template=prompt_template, progress_callback=progress_callback)

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

    def _build_prompt(self, task: Task, spec: str, requirements_text: str) -> str:
        prompt = render_stage_prompt(self.stage_name, spec, self.prompt_template, stage_type=self.stage_type)
        if "文件清单" not in prompt:
            prompt = (prompt.rstrip() + ARCHITECTURE_FILE_LIST_HINT).strip()
        prompt = (prompt.rstrip() + self._stack_guardrail(spec, requirements_text, prompt) + TEXT_OUTPUT_QUALITY_GUARDRAIL).strip()
        return append_prompt_with_runtime_context(prompt, task, self.stage_name)

    def act(self, task: Task, state: SystemState):
        spec = task.context.get("spec", "")
        requirements_text = self._load_requirements_text(task)
        prompt = self._build_prompt(task, spec, requirements_text)
        text = self._generate_text(task, prompt, self.default_action_label)
        failure = _model_failure_text(text)
        if failure:
            raise ValueError(f"{self.failure_prefix}:{failure[:240]}")
        normalized_text = _normalize_architecture_markdown(spec, requirements_text, text)
        return {"type": "md", "filename": "design/architecture.md", "content": normalized_text}


class DocAgent(_BaseGeneratedDocAgent):
    id = "doc-writer"
    capabilities = ["doc.write:v1"]
    default_action_label = "正在生成交付文档"
    failure_prefix = "docs_model_failed"

    def __init__(self, model_adapter, stage_name: str = "docs", stage_type: str = "docs", prompt_template: str | None = None, progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        super().__init__(model_adapter, stage_name=stage_name, stage_type=stage_type, prompt_template=prompt_template, progress_callback=progress_callback)

    def act(self, task: Task, state: SystemState):
        text = self._generate_text(task, self._base_prompt(task), self.default_action_label)
        failure = _model_failure_text(text)
        if failure:
            raise ValueError(f"{self.failure_prefix}:{failure[:240]}")
        return {"type": "md", "filename": "docs/README.md", "content": text}


__all__ = ["ReqAgent", "ArchAgent", "DocAgent"]
