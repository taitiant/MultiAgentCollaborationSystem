from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Tuple

from core import AgentMessage
from orchestration.capability_protocol import summarize_contract


CAPABILITY_INVOKE_FENCE_RE = re.compile(
    r"```capability\.invoke\s*\n(?P<body>[\s\S]*?)```",
    re.IGNORECASE,
)


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_directive(entry: Dict[str, Any]) -> Dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    capability_id = str(entry.get("capability_id") or entry.get("capability") or "").strip()
    if not capability_id:
        return None
    directive = {
        "capability_id": capability_id,
        "input": _as_dict(entry.get("input")),
    }
    binding_id = str(entry.get("binding_id") or entry.get("binding") or "").strip()
    if binding_id:
        directive["binding_id"] = binding_id
    return directive


def extract_capability_invocations_from_text(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    raw = str(text or "")
    directives: List[Dict[str, Any]] = []

    def _replace(match: re.Match[str]) -> str:
        body = str(match.group("body") or "").strip()
        if not body:
            return ""
        try:
            parsed = json.loads(body)
        except Exception:
            return ""
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            normalized = _normalize_directive(item)
            if normalized:
                directives.append(normalized)
        return ""

    cleaned = CAPABILITY_INVOKE_FENCE_RE.sub(_replace, raw)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if raw.endswith("\n") and cleaned:
        cleaned += "\n"
    return cleaned, directives


def extract_capability_invocations(exec_result: Any) -> Tuple[Any, List[Dict[str, Any]]]:
    directives: List[Dict[str, Any]] = []
    if isinstance(exec_result, AgentMessage):
        copied = copy.deepcopy(exec_result)
        for artifact in copied.artifacts:
            if not isinstance(artifact, dict):
                continue
            content = artifact.get("content")
            if not isinstance(content, str):
                continue
            if str(artifact.get("type") or "").strip() in {"json", "capability_request"}:
                continue
            cleaned, artifact_directives = extract_capability_invocations_from_text(content)
            artifact["content"] = cleaned
            directives.extend(artifact_directives)
        return copied, directives

    if isinstance(exec_result, dict):
        copied = copy.deepcopy(exec_result)
        content = copied.get("content")
        if isinstance(content, str):
            cleaned, found = extract_capability_invocations_from_text(content)
            copied["content"] = cleaned
            directives.extend(found)
        return copied, directives

    return exec_result, directives


def build_requested_capability_execution(directives: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    capabilities: List[str] = []
    capability_options: Dict[str, Dict[str, Any]] = {}
    for item in directives or []:
        if not isinstance(item, dict):
            continue
        capability_id = str(item.get("capability_id") or "").strip()
        if not capability_id:
            continue
        if capability_id not in capabilities:
            capabilities.append(capability_id)
        merged = dict(capability_options.get(capability_id) or {})
        merged.update(_as_dict(item.get("input")))
        binding_id = str(item.get("binding_id") or "").strip()
        if binding_id:
            merged["binding_id"] = binding_id
        capability_options[capability_id] = merged
    return capabilities, capability_options


def build_capability_invoke_prompt(capability_defs: List[Dict[str, Any]]) -> str:
    if not capability_defs:
        return ""
    lines: List[str] = [
        "[可调用能力]",
        "如需主动调用特殊能力，请在正常输出末尾追加一个 capability.invoke JSON 代码块；系统会提取并执行，不会把该代码块写入最终文档/代码。",
        "格式：```capability.invoke {\"capability_id\":\"...\",\"input\":{...}} ```",
    ]
    for item in capability_defs:
        summary = summarize_contract(item)
        capability_id = str(summary.get("capability_id") or "")
        if not capability_id:
            continue
        input_fields = ",".join(summary.get("input_fields") or []) or "-"
        output_fields = ",".join(summary.get("output_fields") or []) or "-"
        binding_types = ",".join(summary.get("supported_binding_types") or []) or "内置"
        hint = str(summary.get("invocation_hint") or "").strip()
        lines.append(
            f"- {capability_id}: 输入字段[{input_fields}]；输出字段[{output_fields}]；支持[{binding_types}]；说明：{hint}"
        )
    return "\n".join(lines).strip()


__all__ = [
    "build_capability_invoke_prompt",
    "build_requested_capability_execution",
    "extract_capability_invocations",
    "extract_capability_invocations_from_text",
]
