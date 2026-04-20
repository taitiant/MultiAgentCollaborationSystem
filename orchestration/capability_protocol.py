from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


PROTOCOL_VERSION = "macs.capability.v1"


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_schema(schema: Any) -> Dict[str, Any]:
    raw = _as_dict(schema)
    schema_type = str(raw.get("type") or "object").strip() or "object"
    properties = _as_dict(raw.get("properties"))
    normalized = {
        "type": schema_type,
        "properties": properties,
    }
    required = _as_list(raw.get("required"))
    if required:
        normalized["required"] = required
    if "items" in raw and isinstance(raw.get("items"), dict):
        normalized["items"] = dict(raw.get("items") or {})
    if "description" in raw:
        normalized["description"] = str(raw.get("description") or "").strip()
    return normalized


@dataclass
class CapabilityContract:
    capability_id: str
    label: str
    description: str
    handler: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    invocation_hint: str = ""
    supported_binding_types: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityBindingSpec:
    binding_id: str = ""
    binding_type: str = ""
    required_model_kind: str = ""
    transport: Dict[str, Any] = field(default_factory=dict)
    auth: Dict[str, Any] = field(default_factory=dict)
    model: Dict[str, Any] = field(default_factory=dict)
    workflow: Dict[str, Any] = field(default_factory=dict)
    tool: Dict[str, Any] = field(default_factory=dict)
    mcp: Dict[str, Any] = field(default_factory=dict)
    defaults: Dict[str, Any] = field(default_factory=dict)
    response_contract: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilityInvocationSpec:
    protocol_version: str
    contract: CapabilityContract
    binding: CapabilityBindingSpec
    options: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "contract": self.contract.as_dict(),
            "binding": self.binding.as_dict(),
            "options": dict(self.options or {}),
        }


@dataclass
class CapabilityInvocationRequest:
    protocol_version: str
    task_id: str
    stage_name: str
    stage_type: str
    capability_id: str
    input: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_capability_contract(capability_def: Dict[str, Any]) -> CapabilityContract:
    definition = _as_dict(capability_def)
    capability_id = str(definition.get("id") or "").strip()
    return CapabilityContract(
        capability_id=capability_id,
        label=str(definition.get("label") or capability_id).strip() or capability_id,
        description=str(definition.get("description") or "").strip(),
        handler=str(definition.get("handler") or capability_id).strip() or capability_id,
        input_schema=_normalize_schema(definition.get("input_schema")),
        output_schema=_normalize_schema(definition.get("output_schema")),
        invocation_hint=str(definition.get("invocation_hint") or "").strip(),
        supported_binding_types=_as_list(definition.get("supported_binding_types")),
    )


def build_binding_spec(capability_def: Dict[str, Any], binding: Dict[str, Any] | None = None) -> CapabilityBindingSpec:
    definition = _as_dict(capability_def)
    selected = _as_dict(binding)
    return CapabilityBindingSpec(
        binding_id=str(selected.get("id") or "").strip(),
        binding_type=str(selected.get("binding_type") or "").strip(),
        required_model_kind=str(selected.get("required_model_kind") or definition.get("required_model_kind") or "").strip(),
        transport=_as_dict(selected.get("transport")),
        auth=_as_dict(selected.get("auth")),
        model=_as_dict(selected.get("model")),
        workflow=_as_dict(selected.get("workflow")),
        tool=_as_dict(selected.get("tool")),
        mcp=_as_dict(selected.get("mcp")),
        defaults=_as_dict(selected.get("defaults")),
        response_contract=_as_dict(selected.get("response_contract")),
    )


def build_invocation_spec(
    capability_def: Dict[str, Any],
    binding: Dict[str, Any] | None = None,
    options: Dict[str, Any] | None = None,
) -> CapabilityInvocationSpec:
    return CapabilityInvocationSpec(
        protocol_version=PROTOCOL_VERSION,
        contract=build_capability_contract(capability_def),
        binding=build_binding_spec(capability_def, binding),
        options=_as_dict(options),
    )


def build_invocation_request(
    *,
    task_id: str,
    stage_name: str,
    stage_type: str,
    capability_id: str,
    capability_input: Dict[str, Any] | None = None,
    metadata: Dict[str, Any] | None = None,
) -> CapabilityInvocationRequest:
    return CapabilityInvocationRequest(
        protocol_version=PROTOCOL_VERSION,
        task_id=str(task_id or "").strip(),
        stage_name=str(stage_name or "").strip(),
        stage_type=str(stage_type or "").strip(),
        capability_id=str(capability_id or "").strip(),
        input=_as_dict(capability_input),
        metadata=_as_dict(metadata),
    )


def summarize_contract(capability_def: Dict[str, Any]) -> Dict[str, Any]:
    contract = build_capability_contract(capability_def)
    return {
        "capability_id": contract.capability_id,
        "label": contract.label,
        "input_fields": list((contract.input_schema.get("properties") or {}).keys())[:8],
        "output_fields": list((contract.output_schema.get("properties") or {}).keys())[:8],
        "invocation_hint": contract.invocation_hint,
        "supported_binding_types": contract.supported_binding_types,
    }


__all__ = [
    "PROTOCOL_VERSION",
    "CapabilityBindingSpec",
    "CapabilityContract",
    "CapabilityInvocationRequest",
    "CapabilityInvocationSpec",
    "build_binding_spec",
    "build_capability_contract",
    "build_invocation_request",
    "build_invocation_spec",
    "summarize_contract",
]
