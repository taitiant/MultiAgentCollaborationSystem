"""能力运行时，负责解析绑定、执行处理器并记录能力效果。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core import SystemState, Task
from orchestration.mcp.registry import expand_mcp_binding, merge_mcp_settings
from orchestration.capabilities.adapters import build_adapter_manifest
from orchestration.capabilities.bindings import resolve_capability_binding
from orchestration.capabilities.handlers import (
    AssetGenerateCapabilityHandler,
    CapabilityHandler,
    DocReadCapabilityHandler,
    DocWriteCapabilityHandler,
    ReadmeDeliveryCapabilityHandler,
    binding_execution_plan,
    build_generic_capability_request,
    merge_runtime_options,
)
from orchestration.capabilities.protocol import build_invocation_request, build_invocation_spec
from orchestration.capabilities.registry import get_capability_index


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

    # Helper hooks keep handlers small without reintroducing hard imports.
    result_cls = CapabilityApplyResult

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

    def build_invocation_payload(
        self,
        context,
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
            "binding": binding_execution_plan(context, options) if context.binding() else {},
            "invocation": {
                "spec": spec.as_dict(),
                "request": request.as_dict(),
                "adapter_manifest": build_adapter_manifest(spec, request),
            },
        }
        if extra_payload:
            payload.update(extra_payload)
        return payload


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
            effect["invocation"] = build_invocation_spec(context.capability_def, binding=context.binding(), options=merge_runtime_options(context)).as_dict()
            if not handler:
                if resolved_binding:
                    result = build_generic_capability_request(
                        context,
                        merge_runtime_options(context),
                        context.build_invocation_payload,
                        CapabilityApplyResult,
                    )
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
