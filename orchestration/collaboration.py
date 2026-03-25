from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any, Dict, Iterable, List, Optional

import db
from core import Task, new_event


LOCAL_THREAD_SCOPE = "local"
GLOBAL_THREAD_SCOPE = "global"
ACTIONABLE_MESSAGE_TYPES = {
    "review_feedback",
    "rework_request",
    "smoke_feedback",
    "test_feedback",
    "prerequisite_feedback",
    "user_decision",
}
ACTIONABLE_ENTRY_TYPES = {
    "rework_request",
    "smoke_feedback",
    "test_feedback",
    "prerequisite_gap",
    "stage_review",
    "decision_memory",
    "human_decision_request",
    "user_decision",
}
TARGETED_ENTRY_TYPES = {
    "rework_request",
    "smoke_feedback",
    "test_feedback",
    "stage_review",
    "prerequisite_gap",
    "human_decision_request",
    "user_decision",
}
REVIEW_MESSAGE_TYPES = {
    "test_feedback",
    "prerequisite_feedback",
}
REVIEW_ENTRY_TYPES = {
    "test_feedback",
    "prerequisite_gap",
}
LONG_TERM_ENTRY_TYPES = {
    "decision_memory",
    "stage_review",
}
TEST_FILE_TOKEN_RE = re.compile(r"(?P<path>(?:\.?/)?tests/(?:[\w.\-]+/)*[\w.\-]+\.py)")
BLACKBOARD_PROGRESS_ENTRY_TYPES = {
    "stage_delivery",
    "stage_review",
    "decision_memory",
}
BLACKBOARD_ISSUE_ENTRY_TYPES = {
    "rework_request",
    "smoke_feedback",
    "test_feedback",
    "prerequisite_gap",
    "human_decision_request",
}
BLACKBOARD_SIGNAL_ENTRY_TYPES = BLACKBOARD_PROGRESS_ENTRY_TYPES | BLACKBOARD_ISSUE_ENTRY_TYPES


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


def _blackboard_entry_timestamp(entry: Dict[str, Any]) -> float:
    try:
        return float(entry.get("updated_at") or entry.get("created_at") or 0)
    except Exception:
        return 0.0


def _blackboard_entry_content(entry: Dict[str, Any], *, max_chars: int = 220) -> str:
    text = str(entry.get("content") or "").strip()
    if not text:
        text = str(entry.get("title") or entry.get("entry_key") or "").strip()
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _blackboard_entry_status(entry: Dict[str, Any]) -> str:
    entry_type = str(entry.get("entry_type") or "").strip()
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    if entry_type in BLACKBOARD_ISSUE_ENTRY_TYPES:
        return "settled" if payload.get("resolved") is True else "open"
    if entry_type == "decision_memory":
        return "settled" if payload.get("pass") is True else "info"
    if entry_type == "user_decision":
        return "settled"
    if entry_type == "stage_review":
        if payload.get("pass") is True:
            return "settled"
        if payload.get("pass") is False:
            return "open"
    if entry_type in BLACKBOARD_PROGRESS_ENTRY_TYPES:
        return "progress"
    return "info"


def _is_settled_long_term_entry(entry: Dict[str, Any]) -> bool:
    entry_type = str(entry.get("entry_type") or "").strip()
    if entry_type not in LONG_TERM_ENTRY_TYPES:
        return False
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    return payload.get("pass") is True


def _should_include_prompt_blackboard_entry(stage_name: str, entry: Dict[str, Any]) -> bool:
    entry_type = str(entry.get("entry_type") or "").strip()
    if not entry_type:
        return True
    if _is_settled_long_term_entry(entry):
        return False
    if entry_type == "stage_review":
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        entry_stage = str(entry.get("stage_name") or "")
        if payload.get("pass") is True:
            return False
        return entry_stage == str(stage_name or "")
    return True


def build_blackboard_snapshot(
    entries: List[Dict[str, Any]],
    *,
    fallback_title: str = "共享黑板",
    max_history: int = 6,
    max_open_items: int = 3,
) -> Dict[str, Any]:
    normalized = [entry for entry in (entries or []) if isinstance(entry, dict) and (entry.get("content") or entry.get("title") or entry.get("entry_key"))]
    normalized.sort(key=_blackboard_entry_timestamp, reverse=True)

    latest = normalized[0] if normalized else None
    issue_entries = [entry for entry in normalized if _blackboard_entry_status(entry) == "open"]
    progress_entries = [entry for entry in normalized if str(entry.get("entry_type") or "").strip() in BLACKBOARD_PROGRESS_ENTRY_TYPES]
    positive_review = next(
        (
            entry for entry in progress_entries
            if str(entry.get("entry_type") or "").strip() == "stage_review"
            and isinstance(entry.get("payload"), dict)
            and entry.get("payload", {}).get("pass") is True
        ),
        None,
    )
    focus_entry = issue_entries[0] if issue_entries else (positive_review or (progress_entries[0] if progress_entries else latest))
    latest_update = _blackboard_entry_content(latest or {}, max_chars=220) if latest else ""

    open_items: List[str] = []
    seen_open = set()
    for entry in issue_entries:
        text = _blackboard_entry_content(entry, max_chars=160)
        if not text or text in seen_open:
            continue
        seen_open.add(text)
        open_items.append(text)
        if len(open_items) >= max_open_items:
            break

    final_entry = None
    if not open_items:
        final_entry = positive_review or (progress_entries[0] if progress_entries else latest)

    history = [
        {
            "entry_id": entry.get("entry_id"),
            "entry_key": entry.get("entry_key"),
            "entry_type": entry.get("entry_type"),
            "title": str(entry.get("title") or entry.get("entry_key") or "未命名事项").strip(),
            "summary": _blackboard_entry_content(entry, max_chars=180),
            "status": _blackboard_entry_status(entry),
            "stage_name": entry.get("stage_name"),
            "source_message_id": entry.get("source_message_id"),
            "updated_at": entry.get("updated_at") or entry.get("created_at"),
        }
        for entry in normalized[: max(1, int(max_history or 1))]
    ]

    if open_items:
        board_status = "open"
    elif final_entry:
        board_status = "settled"
    elif latest:
        board_status = "active"
    else:
        board_status = "empty"

    return {
        "title": fallback_title or "共享黑板",
        "status": board_status,
        "entry_count": len(normalized),
        "updated_at": (latest or {}).get("updated_at") or (latest or {}).get("created_at"),
        "shared_context": _blackboard_entry_content(focus_entry or {}, max_chars=240) if focus_entry else "",
        "latest_update": latest_update,
        "open_items": open_items,
        "final_conclusion": _blackboard_entry_content(final_entry or {}, max_chars=240) if final_entry else "",
        "history": history,
    }


def render_blackboard_snapshot_text(snapshot: Dict[str, Any]) -> str:
    if not isinstance(snapshot, dict):
        return ""
    sections: List[str] = []
    shared_context = str(snapshot.get("shared_context") or "").strip()
    if shared_context:
        sections.append("[当前共享结论]\n" + shared_context)
    open_items = [str(item).strip() for item in (snapshot.get("open_items") or []) if str(item).strip()]
    if open_items:
        sections.append("[待处理事项]\n" + "\n".join(f"- {item}" for item in open_items))
    else:
        final_conclusion = str(snapshot.get("final_conclusion") or "").strip()
        if final_conclusion:
            sections.append("[最终结论]\n" + final_conclusion)
    latest_update = str(snapshot.get("latest_update") or "").strip()
    if latest_update and latest_update not in "\n\n".join(sections):
        sections.append("[最近更新]\n" + latest_update)
    return "\n\n".join(section for section in sections if section).strip()


@dataclass
class CollaborationHub:
    task: Task

    def _workspace_root(self) -> str:
        return os.path.abspath(self.task.workspace_path or os.path.join("workspace", self.task.task_id))

    def _stage_definitions(self) -> List[Dict[str, Any]]:
        leader_plan = ((self.task.context or {}).get("leader_plan") or {}) if isinstance(self.task.context, dict) else {}
        raw = leader_plan.get("stages") if isinstance(leader_plan, dict) else None
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict) and item.get("name")]

    def _stage_dependencies(self, stage_name: str) -> List[str]:
        for stage in self._stage_definitions():
            if str(stage.get("name")) != stage_name:
                continue
            deps = stage.get("depends_on")
            return [str(dep) for dep in deps if dep] if isinstance(deps, list) else []
        return []

    def _stage_label(self, stage_name: str) -> str:
        for stage in self._stage_definitions():
            if str(stage.get("name")) == stage_name:
                return str(stage.get("label") or stage_name)
        return stage_name

    def _build_prerequisite_artifact_context(self, stage_name: str, *, max_chars: int = 1800) -> str:
        remaining = max(0, int(max_chars or 0))
        if remaining <= 0:
            return ""
        sections: List[str] = []
        workspace_root = self._workspace_root()
        for dep_stage in self._stage_dependencies(stage_name)[:3]:
            evt = db.get_latest_stage_done_event(self.task.task_id, dep_stage)
            if not evt:
                continue
            payload = evt.get("payload") if isinstance(evt.get("payload"), dict) else {}
            artifacts = payload.get("artifacts") if isinstance(payload, dict) else []
            snippets: List[str] = []
            if isinstance(artifacts, list):
                for art in artifacts[:3]:
                    uri = str((art or {}).get("uri") or "")
                    if not uri or uri == "inline":
                        continue
                    abs_uri = os.path.abspath(uri)
                    if not abs_uri.startswith(workspace_root):
                        continue
                    low = abs_uri.lower()
                    if not low.endswith((".md", ".txt", ".json", ".py", ".html", ".js", ".ts")):
                        continue
                    try:
                        with open(abs_uri, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read(min(remaining, 1200)).strip()
                    except Exception:
                        continue
                    if not content:
                        continue
                    snippets.append(f"[{os.path.relpath(abs_uri, workspace_root)}]\n{content[:min(900, remaining)]}")
                    if len(snippets) >= 2:
                        break
            if not snippets:
                continue
            block = f"[前置阶段产物：{self._stage_label(dep_stage)}]\n" + "\n\n".join(snippets)
            block = block[:remaining]
            if not block:
                break
            sections.append(block)
            remaining -= len(block)
            if remaining <= 0:
                break
        return "\n\n".join(sections).strip()

    def _build_long_term_memory_context(
        self,
        stage_name: str,
        *,
        limit: int = 12,
        max_items: int = 5,
        max_chars: int = 1800,
    ) -> str:
        remaining = max(0, int(max_chars or 0))
        if remaining <= 0:
            return ""
        entries = db.list_blackboard_entries(self.task.task_id, limit=limit)
        selected: List[str] = []
        seen_stage_keys = set()
        for entry in entries:
            if not _is_settled_long_term_entry(entry):
                continue
            entry_stage = str(entry.get("stage_name") or "")
            stage_key = entry_stage or str(entry.get("entry_key") or "")
            if not stage_key or stage_key in seen_stage_keys:
                continue
            line = self._format_blackboard_line(entry)
            if not line:
                continue
            seen_stage_keys.add(stage_key)
            selected.append(line)
            if len(selected) >= max_items:
                break
        if not selected:
            return ""
        text = "\n".join(selected)
        return text[:remaining].strip()

    def _build_test_contract_context(
        self,
        stage_name: str,
        *,
        local_limit: int = 8,
        blackboard_limit: int = 8,
        max_chars: int = 1200,
    ) -> str:
        remaining = max(0, int(max_chars or 0))
        if remaining <= 0:
            return ""

        texts: List[str] = []
        referenced_paths: List[str] = []
        seen_paths = set()
        has_test_signal = False
        workspace_root = self._workspace_root()

        messages = db.list_conversation_messages(self.task.task_id, stage_name=stage_name, limit=local_limit)
        for msg in messages:
            message_type = str(msg.get("message_type") or "").strip()
            if message_type not in ACTIONABLE_MESSAGE_TYPES:
                continue
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            texts.append(content)
            if message_type in {"smoke_feedback", "test_feedback"}:
                has_test_signal = True

        entries = db.list_blackboard_entries(self.task.task_id, stage_name=stage_name, limit=blackboard_limit)
        for entry in entries:
            entry_type = str(entry.get("entry_type") or "").strip()
            if entry_type not in TARGETED_ENTRY_TYPES:
                continue
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            texts.append(content)
            if entry_type in {"smoke_feedback", "test_feedback"}:
                has_test_signal = True

        for text in texts:
            for match in TEST_FILE_TOKEN_RE.finditer(text.replace("`", "")):
                normalized = str(match.group("path") or "").lstrip("./")
                if not normalized or normalized in seen_paths:
                    continue
                abs_path = os.path.join(workspace_root, normalized)
                if os.path.isfile(abs_path):
                    seen_paths.add(normalized)
                    referenced_paths.append(normalized)

        fallback_smoke = os.path.join(workspace_root, "tests", "test_smoke.py")
        if has_test_signal and not referenced_paths and os.path.isfile(fallback_smoke):
            referenced_paths.append("tests/test_smoke.py")

        if not referenced_paths:
            return ""

        sections: List[str] = []
        budget_per_file = max(240, remaining // min(2, len(referenced_paths)))
        for rel_path in referenced_paths[:2]:
            abs_path = os.path.join(workspace_root, rel_path)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    snippet = f.read(max(400, budget_per_file)).strip()
            except Exception:
                continue
            if not snippet:
                continue
            section = f"[{rel_path}]\n{snippet}"
            if len(section) > remaining:
                section = section[:remaining]
            if not section:
                break
            sections.append(section)
            remaining -= len(section)
            if remaining <= 0:
                break

        if not sections:
            return ""
        return "[测试契约]\n" + "\n\n".join(sections)

    @staticmethod
    def _format_blackboard_line(entry: Dict[str, Any], *, include_type_marker: bool = False) -> str:
        entry_type = str(entry.get("entry_type") or "").strip()
        title = str(entry.get("title") or entry.get("entry_key") or "未命名事项").strip()
        content = str(entry.get("content") or "").strip()
        if not content:
            return ""
        marker = f"[{entry_type}] " if include_type_marker and entry_type else ""
        return f"- {marker}{title}: {content}"

    def _emit_event(self, actor_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        evt = new_event(actor_id, self.task.task_id, event_type, payload)
        db.log_event(evt.event_id, evt.task_id, evt.actor_id, evt.event_type, evt.payload, evt.timestamp)

    def ensure_thread(
        self,
        stage_name: str,
        *,
        stage_type: str,
        thread_kind: str = "stage_loop",
        peer_stage: str | None = None,
        title: str | None = None,
        participants: Optional[Iterable[Dict[str, Any]]] = None,
        thread_scope: str = LOCAL_THREAD_SCOPE,
    ) -> str:
        conversation_id = stage_conversation_id(self.task.task_id, stage_name, thread_kind, peer_stage=peer_stage)
        exists = db.list_conversation_messages(self.task.task_id, conversation_id=conversation_id, limit=1)
        if not exists:
            self._emit_event(
                "collaboration",
                "StageConversationStart",
                {
                    "stage": stage_name,
                    "stage_type": stage_type,
                    "conversation_id": conversation_id,
                    "thread_kind": thread_kind,
                    "thread_scope": thread_scope,
                    "peer_stage": peer_stage,
                    "title": title or "",
                    "participants": list(participants or []),
                },
            )
        return conversation_id

    def post_message(
        self,
        *,
        stage_name: str,
        stage_type: str,
        actor_id: str,
        actor_role: str,
        content: str,
        message_type: str,
        conversation_id: str,
        thread_kind: str = "stage_loop",
        thread_scope: str = LOCAL_THREAD_SCOPE,
        recipient_id: str | None = None,
        reply_to: str | None = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        row = db.create_conversation_message(
            self.task.task_id,
            actor_id=actor_id,
            actor_role=actor_role,
            content=content,
            conversation_id=conversation_id,
            stage_name=stage_name,
            stage_type=stage_type,
            thread_kind=thread_kind,
            thread_scope=thread_scope,
            recipient_id=recipient_id,
            message_type=message_type,
            payload=payload,
            reply_to=reply_to,
        )
        self._emit_event(
            actor_id,
            "AgentConversationMessage",
            {
                "stage": stage_name,
                "stage_type": stage_type,
                "conversation_id": conversation_id,
                "thread_kind": thread_kind,
                "thread_scope": thread_scope,
                "message_id": row["message_id"],
                "message_type": message_type,
                "recipient_id": recipient_id,
                "turn_index": row["turn_index"],
                "content_preview": str(content or "")[:280],
            },
        )
        return row

    def upsert_blackboard(
        self,
        *,
        entry_key: str,
        title: str,
        content: str,
        entry_type: str = "note",
        stage_name: str | None = None,
        payload: Optional[Dict[str, Any]] = None,
        source_message_id: str | None = None,
    ) -> Dict[str, Any]:
        row = db.upsert_blackboard_entry(
            self.task.task_id,
            entry_key,
            stage_name=stage_name,
            entry_type=entry_type,
            title=title,
            content=content,
            payload=payload,
            source_message_id=source_message_id,
        )
        self._emit_event(
            "collaboration",
            "GlobalBlackboardUpdated",
            {
                "stage": stage_name,
                "entry_key": entry_key,
                "entry_type": entry_type,
                "title": title,
                "content_preview": str(content or "")[:240],
                "source_message_id": source_message_id,
            },
        )
        return row

    def build_stage_prompt_context(
        self,
        stage_name: str,
        *,
        include_blackboard: bool = True,
        local_limit: int = 8,
        blackboard_limit: int = 8,
        max_chars: int = 5000,
    ) -> str:
        parts: List[str] = []
        remaining = max(0, int(max_chars or 0))

        if remaining > 0:
            long_term_section = self._build_long_term_memory_context(stage_name, max_chars=min(remaining, 1800))
            if long_term_section:
                section = "[长期决策记忆]\n" + long_term_section
                section = section[:remaining]
                parts.append(section)
                remaining -= len(section)

        working_parts: List[str] = []

        if remaining > 0:
            prereq_section = self._build_prerequisite_artifact_context(stage_name, max_chars=min(remaining, 2200))
            if prereq_section:
                prereq_section = prereq_section[:remaining]
                working_parts.append(prereq_section)
                remaining -= len(prereq_section)

        if include_blackboard:
            stage_entries = db.list_blackboard_entries(self.task.task_id, stage_name=stage_name, limit=blackboard_limit)
            global_entries = db.list_blackboard_entries(self.task.task_id, limit=blackboard_limit)
            board_lines = []
            selected_entries: List[Dict[str, Any]] = []
            seen_entry_keys = set()
            for entry in [*stage_entries, *global_entries]:
                entry_key = str(entry.get("entry_key") or "")
                if entry_key and entry_key in seen_entry_keys:
                    continue
                entry_type = str(entry.get("entry_type") or "").strip()
                if entry_type and entry_type not in ACTIONABLE_ENTRY_TYPES:
                    continue
                if not _should_include_prompt_blackboard_entry(stage_name, entry):
                    continue
                line = self._format_blackboard_line(entry)
                if not line:
                    continue
                seen_entry_keys.add(entry_key)
                board_lines.append(line)
                selected_entries.append(entry)
            snapshot_text = render_blackboard_snapshot_text(build_blackboard_snapshot(selected_entries))
            if snapshot_text:
                section = snapshot_text[:remaining]
                if section:
                    working_parts.append(section)
                    remaining -= len(section)
            if board_lines:
                section = "[全局黑板]\n" + "\n".join(board_lines)
                section = section[:remaining]
                if section:
                    working_parts.append(section)
                    remaining -= len(section)

        if remaining > 0:
            messages = db.list_conversation_messages(self.task.task_id, stage_name=stage_name, limit=local_limit)
            convo_lines = []
            for msg in messages:
                message_type = str(msg.get("message_type") or "comment").strip()
                if message_type not in ACTIONABLE_MESSAGE_TYPES:
                    continue
                actor = str(msg.get("actor_role") or msg.get("actor_id") or "unknown").strip()
                content = str(msg.get("content") or "").strip()
                if not content:
                    continue
                convo_lines.append(f"{msg.get('turn_index', '?')}. [{message_type}] {actor}: {content}")
            if convo_lines:
                section = "[局部会话]\n" + "\n".join(convo_lines)
                section = section[:remaining]
                if section:
                    working_parts.append(section)

        if remaining > 0:
            contract_section = self._build_test_contract_context(
                stage_name,
                local_limit=local_limit,
                blackboard_limit=blackboard_limit,
                max_chars=remaining,
            )
            if contract_section:
                working_parts.append(contract_section[:remaining])

        if working_parts:
            working_text = "\n\n".join(part for part in working_parts if part).strip()
            if working_text:
                parts.append("[短期工作记忆]\n" + working_text)

        return "\n\n".join(part for part in parts if part).strip()

    def build_stage_targeted_context(
        self,
        stage_name: str,
        *,
        local_limit: int = 4,
        blackboard_limit: int = 4,
        max_chars: int = 2400,
    ) -> str:
        parts: List[str] = []
        remaining = max(0, int(max_chars or 0))

        if remaining > 0:
            messages = db.list_conversation_messages(self.task.task_id, stage_name=stage_name, limit=local_limit)
            convo_lines = []
            for msg in messages:
                message_type = str(msg.get("message_type") or "comment").strip()
                if message_type not in ACTIONABLE_MESSAGE_TYPES:
                    continue
                actor = str(msg.get("actor_role") or msg.get("actor_id") or "unknown").strip()
                content = str(msg.get("content") or "").strip()
                if not content:
                    continue
                convo_lines.append(f"{msg.get('turn_index', '?')}. [{message_type}] {actor}: {content}")
            if convo_lines:
                section = "[局部会话]\n" + "\n".join(convo_lines)
                section = section[:remaining]
                if section:
                    parts.append(section)
                    remaining -= len(section)

        if remaining > 0:
            stage_entries = db.list_blackboard_entries(self.task.task_id, stage_name=stage_name, limit=blackboard_limit)
            board_lines = []
            selected_entries: List[Dict[str, Any]] = []
            seen_entry_keys = set()
            for entry in stage_entries:
                entry_key = str(entry.get("entry_key") or "")
                if entry_key and entry_key in seen_entry_keys:
                    continue
                entry_type = str(entry.get("entry_type") or "").strip()
                if entry_type and entry_type not in TARGETED_ENTRY_TYPES:
                    continue
                line = self._format_blackboard_line(entry, include_type_marker=True)
                if not line:
                    continue
                seen_entry_keys.add(entry_key)
                board_lines.append(line)
                selected_entries.append(entry)
            snapshot_text = render_blackboard_snapshot_text(build_blackboard_snapshot(selected_entries))
            if snapshot_text:
                section = snapshot_text[:remaining]
                if section:
                    parts.append(section)
                    remaining -= len(section)
            if board_lines:
                section = "[阶段黑板]\n" + "\n".join(board_lines)
                section = section[:remaining]
                if section:
                    parts.append(section)

        return "\n\n".join(part for part in parts if part).strip()

    def build_stage_review_context(
        self,
        stage_name: str,
        *,
        local_limit: int = 4,
        blackboard_limit: int = 4,
        max_chars: int = 2400,
    ) -> str:
        parts: List[str] = []
        remaining = max(0, int(max_chars or 0))

        if remaining > 0:
            stage_entries = db.list_blackboard_entries(self.task.task_id, stage_name=stage_name, limit=blackboard_limit)
            board_lines = []
            selected_entries: List[Dict[str, Any]] = []
            seen_entry_keys = set()
            for entry in stage_entries:
                entry_key = str(entry.get("entry_key") or "")
                if entry_key and entry_key in seen_entry_keys:
                    continue
                entry_type = str(entry.get("entry_type") or "").strip()
                if entry_type and entry_type not in REVIEW_ENTRY_TYPES:
                    continue
                line = self._format_blackboard_line(entry, include_type_marker=True)
                if not line:
                    continue
                seen_entry_keys.add(entry_key)
                board_lines.append(line)
                selected_entries.append(entry)
            snapshot_text = render_blackboard_snapshot_text(build_blackboard_snapshot(selected_entries))
            if snapshot_text:
                section = snapshot_text[:remaining]
                if section:
                    parts.append(section)
                    remaining -= len(section)
            if board_lines:
                section = "[外部反馈]\n" + "\n".join(board_lines)
                section = section[:remaining]
                if section:
                    parts.append(section)
                    remaining -= len(section)

        if remaining > 0:
            messages = db.list_conversation_messages(self.task.task_id, stage_name=stage_name, limit=local_limit)
            convo_lines = []
            for msg in messages:
                message_type = str(msg.get("message_type") or "comment").strip()
                if message_type not in REVIEW_MESSAGE_TYPES:
                    continue
                actor = str(msg.get("actor_role") or msg.get("actor_id") or "unknown").strip()
                content = str(msg.get("content") or "").strip()
                if not content:
                    continue
                convo_lines.append(f"{msg.get('turn_index', '?')}. [{message_type}] {actor}: {content}")
            if convo_lines:
                section = "[外部会话]\n" + "\n".join(convo_lines)
                section = section[:remaining]
                if section:
                    parts.append(section)

        return "\n\n".join(part for part in parts if part).strip()

    @staticmethod
    def summarize_submission(payload: Dict[str, Any]) -> str:
        summary = payload.get("output_summary") or {}
        stage = str(payload.get("stage") or "")
        artifact_count = int(summary.get("artifact_count") or len(payload.get("artifacts") or []))
        artifact_types = [str(item) for item in (summary.get("artifact_types") or []) if item]
        lines = [f"阶段 `{stage}` 已提交新产物。", f"产物数量：{artifact_count}。"]
        if artifact_types:
            lines.append(f"产物类型：{', '.join(artifact_types[:6])}。")
        review = payload.get("review") or {}
        if isinstance(review, dict) and review.get("feedback"):
            lines.append(f"当前评审摘要：{str(review.get('feedback'))[:280]}")
        return "\n".join(lines)

    @staticmethod
    def summarize_review(review: Dict[str, Any]) -> str:
        feedback = str(review.get("feedback") or "").strip()
        next_actions = [str(item).strip() for item in (review.get("next_actions") or []) if str(item).strip()]
        risks = [str(item).strip() for item in (review.get("risks") or []) if str(item).strip()]
        lines = [
            f"评审结论：{'通过' if review.get('pass') else '未通过'}。",
        ]
        if feedback:
            lines.append(f"核心反馈：{feedback}")
        if next_actions:
            lines.append("建议动作：" + "；".join(next_actions[:3]))
        if risks:
            lines.append("风险：" + "；".join(risks[:3]))
        return "\n".join(lines)

    @staticmethod
    def summarize_decision_memory(stage_label: str, payload: Dict[str, Any], review: Dict[str, Any]) -> str:
        summary = payload.get("output_summary") or {}
        result_type = str(summary.get("result_type") or "").strip()
        filename = str(summary.get("filename") or "").strip()
        feedback = str(review.get("feedback") or "").strip()
        next_actions = [str(item).strip() for item in (review.get("next_actions") or []) if str(item).strip()]
        lines = [f"{stage_label} 已通过评审，可作为后续阶段默认依据。"]
        if result_type or filename:
            lines.append(f"确认产物：{filename or '-'}（{result_type or 'artifact'}）。")
        if feedback:
            lines.append(f"确认结论：{feedback[:280]}")
        if next_actions:
            lines.append("后续沿用：" + "；".join(next_actions[:3]))
        return "\n".join(lines)
