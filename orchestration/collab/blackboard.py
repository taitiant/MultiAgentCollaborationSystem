"""共享黑板快照、筛选与文本渲染工具。"""

from __future__ import annotations

import re
from typing import Any, Dict, List


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


def blackboard_entry_status(entry: Dict[str, Any]) -> str:
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


def is_settled_long_term_entry(entry: Dict[str, Any]) -> bool:
    entry_type = str(entry.get("entry_type") or "").strip()
    if entry_type not in LONG_TERM_ENTRY_TYPES:
        return False
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    return payload.get("pass") is True


def should_include_prompt_blackboard_entry(stage_name: str, entry: Dict[str, Any]) -> bool:
    entry_type = str(entry.get("entry_type") or "").strip()
    if not entry_type:
        return True
    if is_settled_long_term_entry(entry):
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
    issue_entries = [entry for entry in normalized if blackboard_entry_status(entry) == "open"]
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
            "status": blackboard_entry_status(entry),
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
