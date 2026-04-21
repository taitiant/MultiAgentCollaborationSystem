"""内置能力处理器以及文档、素材相关的共享辅助函数。"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import zipfile
from io import BytesIO
from typing import Any, Dict, List, Protocol
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import unescape as xml_unescape


class CapabilityHandler(Protocol):
    capability_id: str

    def apply(self, context): ...


def _result_cls(context):
    result_cls = getattr(context, "result_cls", None)
    if result_cls is not None:
        return result_cls
    from orchestration.capabilities.runtime import CapabilityApplyResult

    return CapabilityApplyResult


def merge_runtime_options(context) -> Dict[str, Any]:
    binding = context.binding() or {}
    return {
        **(context.capability_def.get("runtime_defaults") or {}),
        **(binding.get("defaults") or {}),
        **context.options(),
    }


def binding_execution_plan(context, options: Dict[str, Any]) -> Dict[str, Any]:
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


def build_generic_capability_request(context, options: Dict[str, Any], payload_builder, result_cls):
    capability_input = dict(options or {})
    capability_input.pop("binding_id", None)
    capability_input.pop("binding", None)
    request_payload = payload_builder(
        context,
        options,
        action="execute_capability",
        capability_input=capability_input,
    )
    request_filename = f"capabilities/{_safe_capability_slug(context.capability_id)}_{context.stage_name}_request.json"
    return _result_cls(context)(
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


def _guess_document_source_path(context, options: Dict[str, Any]) -> str:
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


def _find_artifact_by_suffix(context, suffixes: tuple[str, ...], preferred_path: str = "") -> Dict[str, Any] | None:
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


def _resolve_document_text(context, options: Dict[str, Any]) -> str:
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


def _document_target_filename(context, options: Dict[str, Any], extension: str, source_path: str = "") -> str:
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

    def apply(self, context):
        options = merge_runtime_options(context)
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
        binding_plan = binding_execution_plan(context, options) if context.binding() else {}
        selected_binding_id = str((binding_plan or {}).get("binding_id") or "")
        if asset_mode in {"image_model", "external_binding"}:
            status = "pending_binding"
            if selected_binding_id:
                status = "binding_ready"
            request_payload = context.build_invocation_payload(
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
        return _result_cls(context)(
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

    def apply(self, context):
        readme_artifact = _find_artifact_by_suffix(context, (".md",), preferred_path="docs/README.md")
        target = str((readme_artifact or {}).get("filename") or (readme_artifact or {}).get("uri") or "docs/README.md")
        return context.result_cls(
            metadata_updates={"delivery_targets": [target]},
            output_summary_updates={"delivery_target": target},
            notes=[f"readme_target={target}"],
            status="applied",
        )


class DocReadCapabilityHandler:
    capability_id = "doc.read:v1"

    def apply(self, context):
        options = merge_runtime_options(context)
        source_path = _guess_document_source_path(context, options)
        source_artifact = _find_artifact_by_suffix(context, (".docx", ".doc", ".md", ".txt"), preferred_path=source_path)
        if not source_artifact:
            return _result_cls(context)(
                notes=["未找到可读取的文档产物。"],
                status="missing_source",
            )
        source_name = str(source_artifact.get("filename") or source_artifact.get("uri") or source_path).replace("\\", "/")
        binding_plan = binding_execution_plan(context, options) if context.binding() else {}
        prefer_external_binding = bool(options.get("prefer_external_binding"))
        if binding_plan and prefer_external_binding:
            request_payload = {
                **context.build_invocation_payload(
                    context,
                    options,
                    action="read_document",
                    capability_input={"source_path": source_name, "extract_mode": str(options.get("extract_mode") or "text")},
                ),
                "source_path": source_name,
            }
            request_name = f"documents/{_basename_without_suffix(source_name)}_read_request.json"
            return context.result_cls(
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
                return _result_cls(context)(
                    notes=["内置仅读取 docx；如需读取 doc，请为该能力配置 HTTP API / 工作流 / 工具绑定。"],
                    status="unsupported_format",
                )
            request_payload = {
                **context.build_invocation_payload(
                    context,
                    options,
                    action="read_document",
                    capability_input={"source_path": source_name},
                ),
                "source_path": source_name,
            }
            return context.result_cls(
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
        return _result_cls(context)(
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

    def apply(self, context):
        options = merge_runtime_options(context)
        output_formats = options.get("output_formats") if isinstance(options.get("output_formats"), list) else []
        if not output_formats:
            output_formats = context.stage_config.get("doc_output_formats") if isinstance(context.stage_config.get("doc_output_formats"), list) else ["docx"]
        notes = [f"output_formats={','.join(output_formats)}"]
        extra_artifacts: List[Dict[str, Any]] = []
        document_text = _resolve_document_text(context, options)
        source_path = _guess_document_source_path(context, options)
        binding_plan = binding_execution_plan(context, options) if context.binding() else {}
        prefer_external_binding = bool(options.get("prefer_external_binding"))
        if binding_plan and prefer_external_binding and document_text.strip():
            target_filename = _document_target_filename(
                context,
                options,
                "docx" if "docx" in output_formats else "doc",
                source_path=source_path,
            )
            request_payload = {
                **context.build_invocation_payload(
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
            return _result_cls(context)(
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
                    **context.build_invocation_payload(
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
        return _result_cls(context)(
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
