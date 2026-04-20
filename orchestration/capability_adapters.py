from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol

from orchestration.capability_protocol import CapabilityInvocationRequest, CapabilityInvocationSpec


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


class CapabilityAdapter(Protocol):
    binding_type: str

    def build_manifest(self, spec: CapabilityInvocationSpec, request: CapabilityInvocationRequest) -> Dict[str, Any]: ...


@dataclass(frozen=True)
class HttpApiCapabilityAdapter:
    binding_type: str = "http_api"

    def build_manifest(self, spec: CapabilityInvocationSpec, request: CapabilityInvocationRequest) -> Dict[str, Any]:
        return {
            "binding_type": self.binding_type,
            "endpoint": {
                "url": spec.binding.transport.get("url") or "",
                "method": spec.binding.transport.get("method") or "POST",
                "headers": _as_dict(spec.binding.transport.get("headers")),
            },
            "request_params": _as_dict(spec.binding.transport.get("request_params")),
            "response_contract": _as_dict(spec.binding.response_contract),
            "request": request.as_dict(),
        }


@dataclass(frozen=True)
class WorkflowApiCapabilityAdapter:
    binding_type: str = "workflow_api"

    def build_manifest(self, spec: CapabilityInvocationSpec, request: CapabilityInvocationRequest) -> Dict[str, Any]:
        return {
            "binding_type": self.binding_type,
            "endpoint": {
                "url": spec.binding.transport.get("url") or "",
                "method": spec.binding.transport.get("method") or "POST",
                "headers": _as_dict(spec.binding.transport.get("headers")),
            },
            "workflow": _as_dict(spec.binding.workflow),
            "request_params": _as_dict(spec.binding.transport.get("request_params")),
            "response_contract": _as_dict(spec.binding.response_contract),
            "request": request.as_dict(),
        }


@dataclass(frozen=True)
class InternalToolCapabilityAdapter:
    binding_type: str = "internal_tool"

    def build_manifest(self, spec: CapabilityInvocationSpec, request: CapabilityInvocationRequest) -> Dict[str, Any]:
        return {
            "binding_type": self.binding_type,
            "tool": _as_dict(spec.binding.tool),
            "request": request.as_dict(),
        }


@dataclass(frozen=True)
class McpServerCapabilityAdapter:
    binding_type: str = "mcp_server"

    def build_manifest(self, spec: CapabilityInvocationSpec, request: CapabilityInvocationRequest) -> Dict[str, Any]:
        return {
            "binding_type": self.binding_type,
            "mcp": _as_dict(spec.binding.mcp),
            "response_contract": _as_dict(spec.binding.response_contract),
            "request": request.as_dict(),
        }


@dataclass(frozen=True)
class DirectModelCapabilityAdapter:
    binding_type: str = "direct_model"

    def build_manifest(self, spec: CapabilityInvocationSpec, request: CapabilityInvocationRequest) -> Dict[str, Any]:
        return {
            "binding_type": self.binding_type,
            "model": _as_dict(spec.binding.model),
            "required_model_kind": spec.binding.required_model_kind,
            "request": request.as_dict(),
        }


ADAPTERS: Dict[str, CapabilityAdapter] = {
    "http_api": HttpApiCapabilityAdapter(),
    "workflow_api": WorkflowApiCapabilityAdapter(),
    "internal_tool": InternalToolCapabilityAdapter(),
    "mcp_server": McpServerCapabilityAdapter(),
    "direct_model": DirectModelCapabilityAdapter(),
}


def resolve_capability_adapter(binding_type: str) -> CapabilityAdapter | None:
    return ADAPTERS.get(str(binding_type or "").strip())


def build_adapter_manifest(spec: CapabilityInvocationSpec, request: CapabilityInvocationRequest) -> Dict[str, Any]:
    adapter = resolve_capability_adapter(spec.binding.binding_type)
    if not adapter:
        return {
            "binding_type": spec.binding.binding_type or "",
            "request": request.as_dict(),
        }
    return adapter.build_manifest(spec, request)


__all__ = [
    "ADAPTERS",
    "CapabilityAdapter",
    "DirectModelCapabilityAdapter",
    "HttpApiCapabilityAdapter",
    "InternalToolCapabilityAdapter",
    "McpServerCapabilityAdapter",
    "WorkflowApiCapabilityAdapter",
    "build_adapter_manifest",
    "resolve_capability_adapter",
]
