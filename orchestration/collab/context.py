"""协作线程命名与运行时提示上下文拼装工具。"""

from __future__ import annotations

from core import Task


LOCAL_THREAD_SCOPE = "local"
GLOBAL_THREAD_SCOPE = "global"


def stage_conversation_id(task_id: str, stage_name: str, thread_kind: str = "stage_loop", peer_stage: str | None = None) -> str:
    parts = [str(task_id or "").strip(), str(stage_name or "").strip(), str(thread_kind or "stage_loop").strip()]
    if peer_stage:
        parts.append(str(peer_stage).strip())
    return "::".join(part for part in parts if part)


def append_prompt_with_runtime_context(prompt: str, task: Task, stage_name: str) -> str:
    runtime = ((task.context or {}).get("_runtime_collaboration") or {}) if isinstance(task.context, dict) else {}
    if runtime.get("stage_name") != stage_name:
        return prompt
    context_text = str(runtime.get("prompt_context") or "").strip()
    if not context_text:
        return prompt
    return f"{prompt.rstrip()}\n\n[阶段协作上下文]\n{context_text}\n"
