from __future__ import annotations

import json
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Protocol
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import unescape as xml_unescape

from core import AgentMessage, SystemState, Task
from orchestration.capability_adapters import build_adapter_manifest
from orchestration.capability_bindings import resolve_capability_binding
from orchestration.mcp_registry import expand_mcp_binding, merge_mcp_settings
from orchestration.capability_protocol import build_invocation_request, build_invocation_spec
from orchestration.capability_registry import get_capability_index


@dataclass
class CapabilityApplyResult:
    extra_artifacts: List[Dict[str, Any]] = field(default_factory=list)
    metadata_updates: Dict[str, Any] = field(default_factory=dict)
    output_summary_updates: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    status: str = "applied"


@dataclass
class CapabilityExecutionContext:
    task: Task
    state: SystemState
    stage_name: str
    stage_type: str
    stage_label: str
    stage_config: Dict[str, Any]
    capability_id: str
    capability_def: Dict[str, Any]
    exec_result: Any
    payload: Dict[str, Any]
    workspace_root: str
    write_artifact: Callable[[Dict[str, Any]], Dict[str, Any]]
    resolved_binding: Optional[Dict[str, Any]] = None
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None

    def options(self) -> Dict[str, Any]:
        raw = self.stage_config.get("capability_options") or {}
        if not isinstance(raw, dict):
            return {}
        return dict(raw.get(self.capability_id) or {})

    def binding(self) -> Optional[Dict[str, Any]]:
        return dict(self.resolved_binding) if isinstance(self.resolved_binding, dict) else None

    def emit_progress(self, **payload: Any) -> None:
        if not self.progress_callback:
            return
        try:
            self.progress_callback(payload)
        except Exception:
            return


class CapabilityHandler(Protocol):
    capability_id: str

    def apply(self, context: CapabilityExecutionContext) -> CapabilityApplyResult: ...


def _merge_capability_runtime_options(context: CapabilityExecutionContext) -> Dict[str, Any]:
    binding = context.binding() or {}
    return {
        **(context.capability_def.get("runtime_defaults") or {}),
        **(binding.get("defaults") or {}),
        **context.options(),
    }


def _binding_execution_plan(context: CapabilityExecutionContext, options: Dict[str, Any]) -> Dict[str, Any]:
    binding = context.binding() or {}
    binding_type = str(binding.get("binding_type") or "internal_tool")
    model = binding.get("model") if isinstance(binding.get("model"), dict) else {}
    transport = binding.get("transport") if isinstance(binding.get("transport"), dict) else {}
    auth = binding.get("auth") if isinstance(binding.get("auth"), dict) else {}
    workflow = binding.get("workflow") if isinstance(binding.get("workflow"), dict) else {}
    tool = binding.get("tool") if isinstance(binding.get("tool"), dict) else {}
    mcp = binding.get("mcp") if isinstance(binding.get("mcp"), dict) else {}
    response_contract = binding.get("response_contract") if isinstance(binding.get("response_contract"), dict) else {}
    return {
        "binding_id": binding.get("id") or "",
        "binding_type": binding_type,
        "capability_id": context.capability_id,
        "required_model_kind": binding.get("required_model_kind") or context.capability_def.get("required_model_kind") or "",
        "transport": transport,
        "auth": auth,
        "model": model,
        "workflow": workflow,
        "tool": tool,
        "mcp": mcp,
        "response_contract": response_contract,
        "options": options,
    }


def _capability_invocation_spec(context: CapabilityExecutionContext, options: Dict[str, Any]) -> Dict[str, Any]:
    return build_invocation_spec(
        context.capability_def,
        binding=context.binding(),
        options=options,
    ).as_dict()


def _capability_invocation_payload(
    context: CapabilityExecutionContext,
    options: Dict[str, Any],
    *,
    action: str,
    capability_input: Dict[str, Any] | None = None,
    extra_payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    spec = build_invocation_spec(context.capability_def, binding=context.binding(), options=options)
    request = build_invocation_request(
        task_id=context.task.task_id,
        stage_name=context.stage_name,
        stage_type=context.stage_type,
        capability_id=context.capability_id,
        capability_input={
            "action": action,
            **(capability_input or {}),
        },
        metadata={
            "stage_label": context.stage_label,
            "workspace_root": context.workspace_root,
        },
    )
    payload = {
        "protocol_version": spec.protocol_version,
        "stage": context.stage_name,
        "capability": context.capability_id,
        "action": action,
        "binding": _binding_execution_plan(context, options) if context.binding() else {},
        "invocation": {
            "spec": spec.as_dict(),
            "request": request.as_dict(),
            "adapter_manifest": build_adapter_manifest(spec, request),
        },
    }
    if extra_payload:
        payload.update(extra_payload)
    return payload


def _load_artifact_text(artifact: Dict[str, Any]) -> str:
    content = artifact.get("content")
    if isinstance(content, str):
        return content
    uri = str(artifact.get("uri") or "")
    if not uri or not os.path.exists(uri):
        return ""
    try:
        with open(uri, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except Exception:
        return ""


def _load_artifact_json(artifact: Dict[str, Any]) -> Dict[str, Any]:
    text = _load_artifact_text(artifact)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _basename_without_suffix(path: str) -> str:
    base = os.path.basename(path)
    if "." not in base:
        return base
    return base.rsplit(".", 1)[0]


def _safe_capability_slug(capability_id: str) -> str:
    raw = unicodedata.normalize("NFKC", str(capability_id or "").strip().lower())
    raw = re.sub(r"[^a-z0-9._-]+", "_", raw)
    raw = raw.strip("._-")
    return raw or "capability"


def _build_generic_capability_request(
    context: CapabilityExecutionContext,
    options: Dict[str, Any],
) -> CapabilityApplyResult:
    capability_input = dict(options or {})
    capability_input.pop("binding_id", None)
    capability_input.pop("binding", None)
    request_payload = _capability_invocation_payload(
        context,
        options,
        action="execute_capability",
        capability_input=capability_input,
    )
    request_filename = f"capabilities/{_safe_capability_slug(context.capability_id)}_{context.stage_name}_request.json"
    return CapabilityApplyResult(
        extra_artifacts=[
            {
                "type": "capability_request",
                "filename": request_filename,
                "content": json.dumps(request_payload, ensure_ascii=False, indent=2),
                "mime": "application/json",
                "capability_id": context.capability_id,
            }
        ],
        metadata_updates={
            "generic_capability_requests": {
                context.capability_id: {
                    "stage_name": context.stage_name,
                    "binding_id": str(((context.binding() or {}).get("id")) or ""),
                    "binding_type": str(((context.binding() or {}).get("binding_type")) or ""),
                    "request_file": request_filename,
                }
            }
        },
        output_summary_updates={"last_generic_capability_request": request_filename},
        notes=["未注册专用处理器，已按绑定生成通用能力执行请求。"],
        status="binding_ready",
    )


def _guess_document_source_path(context: CapabilityExecutionContext, options: Dict[str, Any]) -> str:
    explicit = str(options.get("source_path") or context.stage_config.get("document_source_path") or "").strip()
    if explicit:
        return explicit.replace("\\", "/")
    for artifact in reversed(context.payload.get("artifacts") or []):
        candidate = str(artifact.get("filename") or artifact.get("uri") or "").replace("\\", "/")
        if candidate.lower().endswith((".docx", ".doc", ".md", ".txt")):
            return candidate
    return ""


def _read_binary_artifact(artifact: Dict[str, Any]) -> bytes:
    data = artifact.get("data")
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8", errors="ignore")
    uri = str(artifact.get("uri") or "")
    if not uri or not os.path.exists(uri):
        return b""
    try:
        with open(uri, "rb") as handle:
            return handle.read()
    except Exception:
        return b""


def _find_artifact_by_suffix(context: CapabilityExecutionContext, suffixes: tuple[str, ...], preferred_path: str = "") -> Dict[str, Any] | None:
    preferred = preferred_path.replace("\\", "/").lower()
    artifacts = [artifact for artifact in (context.payload.get("artifacts") or []) if isinstance(artifact, dict)]
    for artifact in artifacts:
        candidate = str(artifact.get("filename") or artifact.get("uri") or "").replace("\\", "/")
        if preferred and candidate.lower() == preferred:
            return artifact
    for artifact in reversed(artifacts):
        candidate = str(artifact.get("filename") or artifact.get("uri") or "").replace("\\", "/").lower()
        if candidate.endswith(suffixes):
            return artifact
    return None


def _extract_text_from_docx_bytes(data: bytes) -> str:
    if not data:
        return ""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception:
        return ""
    chunks = re.findall(r"<w:t[^>]*>(.*?)</w:t>", document_xml, flags=re.IGNORECASE | re.DOTALL)
    text = "\n".join(xml_unescape(chunk) for chunk in chunks if chunk)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _resolve_document_text(context: CapabilityExecutionContext, options: Dict[str, Any]) -> str:
    explicit_text = options.get("content")
    if isinstance(explicit_text, str) and explicit_text.strip():
        return explicit_text
    preferred_path = _guess_document_source_path(context, options)
    artifact = _find_artifact_by_suffix(context, (".docx", ".md", ".txt"), preferred_path=preferred_path)
    if artifact:
        candidate = str(artifact.get("filename") or artifact.get("uri") or "").lower()
        if candidate.endswith(".docx"):
            return _extract_text_from_docx_bytes(_read_binary_artifact(artifact))
        return _load_artifact_text(artifact)
    return ""


def _document_target_filename(context: CapabilityExecutionContext, options: Dict[str, Any], extension: str, source_path: str = "") -> str:
    explicit = str(options.get("target_filename") or context.stage_config.get("document_target_filename") or "").strip()
    if explicit:
        root, _sep, _suffix = explicit.rpartition(".")
        if root:
            return f"{root}.{extension}"
        return explicit if explicit.lower().endswith(f".{extension}") else f"{explicit}.{extension}"
    base_source = source_path or _guess_document_source_path(context, options) or f"documents/{context.stage_name}"
    if "." in os.path.basename(base_source):
        base_source = base_source.rsplit(".", 1)[0]
    return f"{base_source}.{extension}"


def _docx_from_markdown(markdown_text: str, title: str = "") -> bytes:
    raw_lines = [line.rstrip() for line in str(markdown_text or "").splitlines()]
    paragraphs = [line for line in raw_lines if line.strip()]
    if title:
        paragraphs = [title, *paragraphs]
    if not paragraphs:
        paragraphs = ["Document"]

    body_parts: List[str] = []
    for line in paragraphs:
        text = line
        if text.startswith("#"):
            text = text.lstrip("#").strip()
        elif text.startswith(("- ", "* ")):
            text = f"• {text[2:].strip()}"
        body_parts.append(
            "<w:p><w:r><w:t xml:space=\"preserve\">"
            + xml_escape(text)
            + "</w:t></w:r></w:p>"
        )
    document_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:wpc=\"http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas\" "
        "xmlns:mc=\"http://schemas.openxmlformats.org/markup-compatibility/2006\" "
        "xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\" "
        "xmlns:m=\"http://schemas.openxmlformats.org/officeDocument/2006/math\" "
        "xmlns:v=\"urn:schemas-microsoft-com:vml\" "
        "xmlns:wp14=\"http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing\" "
        "xmlns:wp=\"http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing\" "
        "xmlns:w10=\"urn:schemas-microsoft-com:office:word\" "
        "xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\" "
        "xmlns:w14=\"http://schemas.microsoft.com/office/word/2010/wordml\" "
        "xmlns:w15=\"http://schemas.microsoft.com/office/word/2012/wordml\" "
        "xmlns:wpg=\"http://schemas.microsoft.com/office/word/2010/wordprocessingGroup\" "
        "xmlns:wpi=\"http://schemas.microsoft.com/office/word/2010/wordprocessingInk\" "
        "xmlns:wne=\"http://schemas.microsoft.com/office/2006/wordml\" "
        "xmlns:wps=\"http://schemas.microsoft.com/office/word/2010/wordprocessingShape\" mc:Ignorable=\"w14 w15 wp14\">"
        "<w:body>"
        + "".join(body_parts)
        + "<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/><w:pgMar w:top=\"1440\" w:right=\"1440\" w:bottom=\"1440\" w:left=\"1440\" w:header=\"720\" w:footer=\"720\" w:gutter=\"0\"/></w:sectPr>"
        "</w:body></w:document>"
    )
    content_types = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
        "</Types>"
    )
    rels = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"word/document.xml\"/>"
        "</Relationships>"
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


class AssetGenerateCapabilityHandler:
    capability_id = "asset.generate:v1"

    def apply(self, context: CapabilityExecutionContext) -> CapabilityApplyResult:
        options = _merge_capability_runtime_options(context)
        asset_mode = str(options.get("asset_mode") or context.stage_config.get("asset_mode") or "svg_placeholder")
        output_formats = options.get("output_formats") if isinstance(options.get("output_formats"), list) else ["svg"]
        manifest_artifact = next(
            (
                artifact for artifact in (context.payload.get("artifacts") or [])
                if str(artifact.get("uri") or artifact.get("filename") or "").replace("\\", "/").endswith("assets/manifest.json")
            ),
            None,
        )
        manifest = _load_artifact_json(manifest_artifact or {})
        status = "placeholder_ready"
        notes = [f"asset_mode={asset_mode}"]
        extra_artifacts: List[Dict[str, Any]] = []
        binding_plan = _binding_execution_plan(context, options) if context.binding() else {}
        selected_binding_id = str((binding_plan or {}).get("binding_id") or "")
        if asset_mode in {"image_model", "external_binding"}:
            status = "pending_binding"
            if selected_binding_id:
                status = "binding_ready"
            request_payload = _capability_invocation_payload(
                context,
                options,
                action="generate_assets",
                capability_input={
                    "asset_mode": asset_mode,
                    "output_formats": output_formats,
                    "assets": manifest.get("assets") or [],
                },
                extra_payload={
                    "asset_mode": asset_mode,
                    "output_formats": output_formats,
                    "assets": manifest.get("assets") or [],
                },
            )
            extra_artifacts.append(
                {
                    "type": "capability_request",
                    "filename": "assets/image_generation_request.json",
                    "content": json.dumps(request_payload, ensure_ascii=False, indent=2),
                    "mime": "application/json",
                    "capability_id": context.capability_id,
                }
            )
            notes.append("已生成能力执行请求清单")
        if selected_binding_id:
            notes.append(f"binding={selected_binding_id}")
        return CapabilityApplyResult(
            extra_artifacts=extra_artifacts,
            metadata_updates={
                "asset_generation": {
                    "mode": asset_mode,
                    "output_formats": output_formats,
                    "binding_id": selected_binding_id,
                    "binding_type": str((binding_plan or {}).get("binding_type") or ""),
                }
            },
            output_summary_updates={"capability_asset_mode": asset_mode},
            notes=notes,
            status=status,
        )


class ReadmeDeliveryCapabilityHandler:
    capability_id = "delivery.readme:v1"

    def apply(self, context: CapabilityExecutionContext) -> CapabilityApplyResult:
        readme_artifact = _find_artifact_by_suffix(context, (".md",), preferred_path="docs/README.md")
        target = str((readme_artifact or {}).get("filename") or (readme_artifact or {}).get("uri") or "docs/README.md")
        return CapabilityApplyResult(
            metadata_updates={"delivery_targets": [target]},
            output_summary_updates={"delivery_target": target},
            notes=[f"readme_target={target}"],
            status="applied",
        )


class DocReadCapabilityHandler:
    capability_id = "doc.read:v1"

    def apply(self, context: CapabilityExecutionContext) -> CapabilityApplyResult:
        options = _merge_capability_runtime_options(context)
        source_path = _guess_document_source_path(context, options)
        source_artifact = _find_artifact_by_suffix(context, (".docx", ".doc", ".md", ".txt"), preferred_path=source_path)
        if not source_artifact:
            return CapabilityApplyResult(
                notes=["未找到可读取的文档产物。"],
                status="missing_source",
            )
        source_name = str(source_artifact.get("filename") or source_artifact.get("uri") or source_path).replace("\\", "/")
        binding_plan = _binding_execution_plan(context, options) if context.binding() else {}
        prefer_external_binding = bool(options.get("prefer_external_binding"))
        if binding_plan and prefer_external_binding:
            request_payload = {
                **_capability_invocation_payload(
                    context,
                    options,
                    action="read_document",
                    capability_input={"source_path": source_name, "extract_mode": str(options.get("extract_mode") or "text")},
                ),
                "source_path": source_name,
            }
            request_name = f"documents/{_basename_without_suffix(source_name)}_read_request.json"
            return CapabilityApplyResult(
                extra_artifacts=[
                    {
                        "type": "capability_request",
                        "filename": request_name,
                        "content": json.dumps(request_payload, ensure_ascii=False, indent=2),
                        "mime": "application/json",
                        "capability_id": context.capability_id,
                    }
                ],
                metadata_updates={"document_read": {"source_path": source_name, "binding_id": binding_plan.get("binding_id") or "", "request_file": request_name}},
                notes=["已按配置转为外部文档读取请求。"],
                status="binding_ready",
            )
        if source_name.lower().endswith(".doc"):
            if not binding_plan:
                return CapabilityApplyResult(
                    notes=["内置仅读取 docx；如需读取 doc，请为该能力配置 HTTP API / 工作流 / 工具绑定。"],
                    status="unsupported_format",
                )
            request_payload = {
                **_capability_invocation_payload(
                    context,
                    options,
                    action="read_document",
                    capability_input={"source_path": source_name},
                ),
                "source_path": source_name,
            }
            return CapabilityApplyResult(
                extra_artifacts=[
                    {
                        "type": "capability_request",
                        "filename": f"documents/{_basename_without_suffix(source_name)}_read_request.json",
                        "content": json.dumps(request_payload, ensure_ascii=False, indent=2),
                        "mime": "application/json",
                        "capability_id": context.capability_id,
                    }
                ],
                metadata_updates={"document_read": {"source_path": source_name, "binding_id": binding_plan.get("binding_id") or ""}},
                notes=["doc 读取已转为外部能力请求清单。"],
                status="binding_ready",
            )
        text = _resolve_document_text(context, options)
        output_filename = _document_target_filename(context, {**options, "target_filename": f"documents/{_basename_without_suffix(source_name)}.txt"}, "txt", source_path=source_name)
        return CapabilityApplyResult(
            extra_artifacts=[
                {
                    "type": "txt",
                    "filename": output_filename,
                    "content": text,
                    "mime": "text/plain",
                    "capability_id": context.capability_id,
                }
            ],
            metadata_updates={"document_read": {"source_path": source_name, "output_path": output_filename}},
            output_summary_updates={"document_read_output": output_filename},
            notes=[f"source={source_name}", f"output={output_filename}"],
            status="applied" if text else "empty",
        )


class DocWriteCapabilityHandler:
    capability_id = "doc.write:v1"

    def apply(self, context: CapabilityExecutionContext) -> CapabilityApplyResult:
        options = _merge_capability_runtime_options(context)
        output_formats = options.get("output_formats") if isinstance(options.get("output_formats"), list) else []
        if not output_formats:
            output_formats = context.stage_config.get("doc_output_formats") if isinstance(context.stage_config.get("doc_output_formats"), list) else ["docx"]
        notes = [f"output_formats={','.join(output_formats)}"]
        extra_artifacts: List[Dict[str, Any]] = []
        document_text = _resolve_document_text(context, options)
        source_path = _guess_document_source_path(context, options)
        binding_plan = _binding_execution_plan(context, options) if context.binding() else {}
        prefer_external_binding = bool(options.get("prefer_external_binding"))
        if binding_plan and prefer_external_binding and document_text.strip():
            target_filename = _document_target_filename(
                context,
                options,
                "docx" if "docx" in output_formats else "doc",
                source_path=source_path,
            )
            request_payload = {
                **_capability_invocation_payload(
                    context,
                    options,
                    action="write_document",
                    capability_input={
                        "target_filename": target_filename,
                        "content": document_text,
                        "output_formats": output_formats,
                    },
                ),
                "target_filename": target_filename,
                "content": document_text,
            }
            request_name = f"documents/{_basename_without_suffix(target_filename)}_write_request.json"
            extra_artifacts.append(
                {
                    "type": "capability_request",
                    "filename": request_name,
                    "content": json.dumps(request_payload, ensure_ascii=False, indent=2),
                    "mime": "application/json",
                    "capability_id": context.capability_id,
                }
            )
            notes.append("已按配置转为外部文档写入请求")
            return CapabilityApplyResult(
                extra_artifacts=extra_artifacts,
                metadata_updates={
                    "doc_outputs": output_formats,
                    "document_write": {
                        "source_path": source_path,
                        "output_formats": output_formats,
                        "binding_id": binding_plan.get("binding_id") or "",
                        "request_file": request_name,
                    },
                },
                output_summary_updates={"doc_output_formats": output_formats},
                notes=notes,
                status="binding_ready",
            )
        if "docx" in output_formats and document_text.strip():
            docx_name = _document_target_filename(context, options, "docx", source_path=source_path)
            extra_artifacts.append(
                {
                    "type": "docx",
                    "filename": docx_name,
                    "data": _docx_from_markdown(document_text, title=str(options.get("title") or context.stage_label or "")),
                    "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "capability_id": context.capability_id,
                }
            )
            notes.append("已生成 docx 文档")
        if "doc" in output_formats:
            if binding_plan:
                request_payload = {
                    **_capability_invocation_payload(
                        context,
                        options,
                        action="write_document",
                        capability_input={
                            "target_filename": _document_target_filename(context, options, "doc", source_path=source_path),
                            "content": document_text,
                            "output_formats": output_formats,
                        },
                    ),
                    "target_filename": _document_target_filename(context, options, "doc", source_path=source_path),
                    "content": document_text,
                }
                extra_artifacts.append(
                    {
                        "type": "capability_request",
                        "filename": "documents/document_write_request.json",
                        "content": json.dumps(request_payload, ensure_ascii=False, indent=2),
                        "mime": "application/json",
                        "capability_id": context.capability_id,
                    }
                )
                notes.append("doc 写入已转为外部能力请求清单")
            else:
                notes.append("doc 二进制写入需要绑定外部 HTTP API / 工作流 / 工具")
        return CapabilityApplyResult(
            extra_artifacts=extra_artifacts,
            metadata_updates={
                "doc_outputs": output_formats,
                "document_write": {
                    "source_path": source_path,
                    "output_formats": output_formats,
                },
            },
            output_summary_updates={"doc_output_formats": output_formats},
            notes=notes,
            status="binding_ready" if any(artifact.get("type") == "capability_request" for artifact in extra_artifacts) else "applied",
        )


BUILTIN_CAPABILITY_HANDLERS: Dict[str, CapabilityHandler] = {
    "delivery.readme:v1": ReadmeDeliveryCapabilityHandler(),
    "asset.generate:v1": AssetGenerateCapabilityHandler(),
    "doc.read:v1": DocReadCapabilityHandler(),
    "doc.write:v1": DocWriteCapabilityHandler(),
}


class CapabilityRuntime:
    def __init__(
        self,
        capability_settings: Dict[str, Any] | None = None,
        handlers: Optional[Dict[str, CapabilityHandler]] = None,
        mcp_settings: Dict[str, Any] | None = None,
    ):
        self.capability_settings = capability_settings or {}
        self.capability_index = get_capability_index(self.capability_settings)
        self.handlers = handlers or BUILTIN_CAPABILITY_HANDLERS
        self.mcp_settings = merge_mcp_settings(mcp_settings)

    def _resolve_runtime_binding(self, capability_id: str, requested_binding_id: str = "") -> Dict[str, Any] | None:
        binding = resolve_capability_binding(
            capability_id,
            self.capability_settings,
            requested_binding_id=requested_binding_id,
        )
        if not isinstance(binding, dict):
            return None
        if str(binding.get("binding_type") or "") == "mcp_server":
            binding["mcp"] = expand_mcp_binding(binding.get("mcp"), self.mcp_settings)
        return binding

    def apply_stage_capabilities(
        self,
        *,
        task: Task,
        state: SystemState,
        stage_name: str,
        stage_type: str,
        stage_label: str,
        stage_config: Dict[str, Any],
        capabilities: List[str],
        exec_result: Any,
        payload: Dict[str, Any],
        write_artifact: Callable[[Dict[str, Any]], Dict[str, Any]],
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        capability_effects: List[Dict[str, Any]] = list(payload.get("capability_effects") or [])
        payload.setdefault("metadata", {})
        payload.setdefault("output_summary", {})
        workspace_root = os.path.abspath(task.workspace_path or "")
        for capability_id in [str(item).strip() for item in (capabilities or []) if str(item).strip()]:
            capability_def = dict(self.capability_index.get(capability_id) or {"id": capability_id})
            handler = self.handlers.get(capability_id)
            raw_options = stage_config.get("capability_options") if isinstance(stage_config.get("capability_options"), dict) else {}
            capability_options = raw_options.get(capability_id) if isinstance(raw_options.get(capability_id), dict) else {}
            requested_binding_id = str(capability_options.get("binding_id") or capability_options.get("binding") or "").strip()
            resolved_binding = self._resolve_runtime_binding(capability_id, requested_binding_id=requested_binding_id)
            effect = {
                "capability_id": capability_id,
                "handler": capability_def.get("handler") or capability_id,
                "required_model_kind": capability_def.get("required_model_kind") or "",
                "binding_id": str((resolved_binding or {}).get("id") or ""),
                "binding_type": str((resolved_binding or {}).get("binding_type") or ""),
                "status": "skipped" if not handler else "applied",
                "notes": [],
            }
            if capability_def.get("enabled", True) is False:
                effect["status"] = "disabled"
                effect["notes"] = ["能力已禁用，不参与运行时执行。"]
                capability_effects.append(effect)
                continue
            context = CapabilityExecutionContext(
                task=task,
                state=state,
                stage_name=stage_name,
                stage_type=stage_type,
                stage_label=stage_label,
                stage_config=stage_config,
                capability_id=capability_id,
                capability_def=capability_def,
                exec_result=exec_result,
                payload=payload,
                workspace_root=workspace_root,
                write_artifact=write_artifact,
                resolved_binding=resolved_binding,
                progress_callback=progress_callback,
            )
            effect["invocation"] = _capability_invocation_spec(context, _merge_capability_runtime_options(context))
            if not handler:
                if resolved_binding:
                    result = _build_generic_capability_request(context, _merge_capability_runtime_options(context))
                    for artifact in result.extra_artifacts:
                        written = write_artifact(artifact)
                        payload.setdefault("artifacts", []).append(written)
                    payload["metadata"].update(result.metadata_updates or {})
                    payload["output_summary"].update(result.output_summary_updates or {})
                    effect["status"] = result.status or "binding_ready"
                    effect["notes"] = list(result.notes or [])
                else:
                    effect["notes"] = ["未注册能力处理器，当前仅作为规划/路由能力使用。"]
                capability_effects.append(effect)
                continue
            context.emit_progress(
                capability_id=capability_id,
                capability_label=capability_def.get("label") or capability_id,
                progress_state="start",
                message=f"开始执行能力 {capability_def.get('label') or capability_id}",
            )
            try:
                result = handler.apply(context)
            except Exception as exc:
                effect["status"] = "error"
                effect["notes"] = [f"能力执行失败：{exc}"]
                context.emit_progress(
                    capability_id=capability_id,
                    capability_label=capability_def.get("label") or capability_id,
                    progress_state="error",
                    message=f"能力 {capability_def.get('label') or capability_id} 执行失败",
                    error=str(exc),
                )
                capability_effects.append(effect)
                continue
            for artifact in result.extra_artifacts:
                written = write_artifact(artifact)
                payload.setdefault("artifacts", []).append(written)
            payload["metadata"].update(result.metadata_updates or {})
            payload["output_summary"].update(result.output_summary_updates or {})
            effect["status"] = result.status or "applied"
            effect["notes"] = list(result.notes or [])
            context.emit_progress(
                capability_id=capability_id,
                capability_label=capability_def.get("label") or capability_id,
                progress_state="done",
                message=f"能力 {capability_def.get('label') or capability_id} 执行完成",
                status=effect["status"],
                notes=effect["notes"],
            )
            capability_effects.append(effect)
        if capability_effects:
            payload["capability_effects"] = capability_effects
            payload["metadata"]["capability_effects"] = capability_effects
            payload["output_summary"]["capability_count"] = len(capability_effects)
        return payload


__all__ = ["CapabilityRuntime", "CapabilityExecutionContext", "CapabilityApplyResult"]
