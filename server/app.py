"""FastAPI 服务入口，负责把 HTTP 路由接到任务运行时与应用服务。"""

from __future__ import annotations
import asyncio
import json
import os
import shutil
import uuid
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import (
    Task,
    new_event,
)
from orchestration.capabilities.registry import (
    get_default_capability_catalog,
    merge_capability_settings,
    sync_capability_settings_with_skills,
)
from orchestration.graph_builder import (
    init_task_workspace,
)
from orchestration.collab import CollaborationHub, build_blackboard_snapshot
from orchestration.mcp.registry import merge_mcp_settings
from orchestration.planning.stage_catalog import (
    DEFAULT_STAGE_PROMPTS,
    STAGE_TYPE_BLUEPRINTS,
    build_stage_type_blueprints,
    normalize_execution_profile,
    normalize_stage_semantics,
    normalize_stage_type,
    render_stage_prompt,
    resolve_stage_execution_profile,
    resolve_stage_semantics,
)
from orchestration.skills.registry import get_default_skill_catalog, merge_skill_settings
from orchestration.planning.workflow_plan import (
    resolve_conversation_groups,
    write_leader_plan_snapshot,
)
from orchestration.application.tasks import TaskApplicationService
from orchestration.bootstrap.container import build_app_container
from orchestration.planning.workspace_cleanup import cleanup_architecture_orphan_files
from server.schemas import (
    AiModelTestRequest,
    AiModelTestResponse,
    AiStageBindingsResponse,
    CreateAiCredentialRequest,
    CreateAiModelRequest,
    CreateTaskRequest,
    ModelTestRequest,
    ProviderTestResponse,
    SetTaskModelResponse,
    SetTaskModelRequest,
    SetCapabilitiesRequest,
    SetMcpServersRequest,
    SetSkillsRequest,
    SimpleStatusResponse,
    SubmitHumanDecisionRequest,
    QuickCreateAiModelRequest,
    TaskStatusResponse,
    TaskWorkspaceResponse,
    UpdateAiCredentialRequest,
    UpdateAiModelRequest,
    UpdateAiStageBindingsRequest,
    UpdateTaskEventConfigRequest,
    UpdateTaskEventConfigResponse,
)
import db

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
container = build_app_container(BASE_DIR)
CAPA_CONFIG_PATH = container.config.paths.capability_config_path
MCP_CONFIG_PATH = container.config.paths.mcp_config_path
SKILL_CONFIG_PATH = container.config.paths.skill_config_path
WORKSPACE_ROOT = container.config.paths.workspace_root
WF_TPL = container.config.paths.workflow_template_path

app = FastAPI(title="MACS Runtime")

WORKFLOW_TEMPLATE = container.config.workflow_template
WORKFLOW_STAGES = container.config.workflow_stages
WORKFLOW_STAGE_MAP = container.config.workflow_stage_map
EXECUTION_PROFILE_KEYS = container.config.execution_profile_keys
BINDABLE_STAGE_TYPES = EXECUTION_PROFILE_KEYS
STAGE_RUNTIME_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "assets": {
        "asset_mode": "svg_placeholder",
        "max_asset_count": 6,
        "image_model_provider": "",
        "review_blocking": True,
    },
    "coding": {
        "auto_rework_limit": 2,
        "auto_smoke_fix_limit": 3,
        "smoke_test_blocking": True,
        "rework_cleanup": False,
        "targeted_rework_enabled": True,
    },
    "testing": {
        "auto_fix_limit": 4,
        "review_blocking": False,
        "test_command": "pytest -q",
    },
    "docs": {
        "auto_rework_limit": 2,
        "doc_output_formats": ["md"],
    },
}

storage = container.infrastructure.storage
model_registry = container.infrastructure.model_registry
logging_plugin = container.infrastructure.logging_plugin
metrics_plugin = container.infrastructure.metrics_plugin
CAPA_CONFIG = container.config.capability_config
MCP_CONFIG = container.config.mcp_config
SKILL_CONFIG = container.config.skill_config
graph_builder = container.execution.graph_builder

# ---- 运行时状态 ----
runtime = container.execution.runtime
state = runtime.state


def is_task_aborted(task_id: str) -> bool:
    return runtime.is_task_aborted(task_id)


def mark_task_aborted(task_id: str):
    runtime.mark_task_aborted(task_id)


def clear_task_abort(task_id: str):
    runtime.clear_task_abort(task_id)


workflow_runner = container.execution.workflow_runner


def _persist_json(path: str, payload: Dict[str, Any]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _sync_capability_config_with_skills(persist: bool = False) -> Dict[str, Any]:
    merged = sync_capability_settings_with_skills(CAPA_CONFIG, SKILL_CONFIG)
    CAPA_CONFIG.clear()
    CAPA_CONFIG.update(merged)
    if persist:
        _persist_json(CAPA_CONFIG_PATH, CAPA_CONFIG)
    return merged


def _create_task_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    task_id = body.get("task_id") or str(uuid.uuid4())
    required_caps = body.get("required_capabilities") or []
    context = body.get("context") or {}
    context.setdefault("default_model_provider", get_default_provider_id())
    context.setdefault("event_configs", {})
    priority = int(body.get("priority", 50))
    domain = body.get("domain", "software")
    workspace_path = body.get("workspace_path") or os.path.join(WORKSPACE_ROOT, task_id)

    task = Task(
        task_id=task_id,
        domain=domain,
        required_capabilities=required_caps,
        context=context,
        priority=priority,
        workspace_path=workspace_path,
    )
    ensure_task_defaults(task)
    state.tasks[task_id] = task
    state.task_status[task_id] = "created"
    db.save_task(task_id, domain, required_caps, task.context, priority, workspace_path, "created")
    init_task_workspace(WORKSPACE_ROOT, task)
    evt = new_event("user", task_id, "TaskCreated", task.__dict__)
    _record_event(evt)
    return {"task_id": task_id, "workspace_path": workspace_path}


def _abort_task_impl(task_id: str) -> Dict[str, Any]:
    mark_task_aborted(task_id)
    runtime.set_task_status(task_id, "aborting")
    evt = new_event("user", task_id, "TaskAbortRequested", {"task_id": task_id})
    _record_event(evt)
    return {"status": "aborting", "task_id": task_id}


def _set_task_status(task_id: str, status: str) -> str:
    return runtime.set_task_status(task_id, status)


def _record_event(event):
    return runtime.record_event(event)


def _payload_dict(body: Any) -> Dict[str, Any]:
    # 保持路由函数在测试里也能直接接收普通 dict，而不强依赖 FastAPI 的请求解析。
    if isinstance(body, BaseModel):
        return body.model_dump(exclude_unset=True)
    if isinstance(body, dict):
        return dict(body)
    return {}


def _merge_presented_task_status(persisted_status: str, runtime_status: str) -> str:
    persisted = str(persisted_status or "").strip()
    runtime = str(runtime_status or "").strip()
    if not runtime:
        return persisted
    if runtime == persisted:
        return runtime
    # 运行时状态对临时执行态最有价值，但不应覆盖数据库里更“新鲜”的终态，例如 completed。
    if runtime in {"created", "running", "aborting", "waiting_user"}:
        return runtime
    if not persisted or persisted == "unknown":
        return runtime
    return persisted


def _complete_task(task: Task, artifacts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    _set_task_status(task.task_id, "completed")
    db.update_task_context(task.task_id, task.context)
    return {"status": "completed", "artifacts": artifacts or []}


def _pending_human_decision(task: Task) -> Dict[str, Any] | None:
    pending = (task.context or {}).get("pending_human_decision") if isinstance(task.context, dict) else None
    return dict(pending) if isinstance(pending, dict) and pending.get("kind") == "human_decision" else None


def _clear_pending_human_decision(task: Task) -> None:
    if isinstance(task.context, dict):
        task.context.pop("pending_human_decision", None)


def _cleanup_latest_stage_artifacts(task: Task, stage_name: str):
    workspace = os.path.abspath(task.workspace_path or os.path.join(WORKSPACE_ROOT, task.task_id))
    try:
        stage_map = task_stage_map(task)
        stage_def = stage_map.get(stage_name) or {"name": stage_name, "stage_type": stage_name}
    except Exception:
        stage_def = {"name": stage_name, "stage_type": stage_name}
    return _cleanup_stage_artifacts_by_scope(task, workspace, stage_name, stage_def)


def _latest_stage_done_payload(task_id: str, stage_name: str) -> Dict[str, Any]:
    try:
        target = db.get_latest_stage_done_event(task_id, stage_name)
    except Exception:
        target = None
    if target and isinstance(target.get("payload"), dict):
        return dict(target.get("payload") or {})

    for evt in reversed(state.history):
        if getattr(evt, "task_id", None) != task_id:
            continue
        if getattr(evt, "event_type", "") != "StageDone":
            continue
        payload = getattr(evt, "payload", None) or {}
        if payload.get("stage") != stage_name:
            continue
        return dict(payload)
    return {}


def _latest_stage_artifacts(task_id: str, stage_name: str) -> List[Dict[str, Any]]:
    payload = _latest_stage_done_payload(task_id, stage_name)
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    return [dict(item) for item in artifacts if isinstance(item, dict)]


def _stage_cleanup_roots(stage_type: str) -> set[str]:
    normalized = normalize_execution_profile(stage_type)
    if normalized == "requirements":
        return {"analysis"}
    if normalized == "architecture":
        return {"design"}
    if normalized == "assets":
        return {"assets", "design"}
    if normalized == "docs":
        return {"docs"}
    if normalized == "testing":
        return {"tests"}
    if normalized == "coding":
        return {"code", "app", "src", "public", "scripts", "styles", "tests"}
    return set()


def _cleanup_stage_artifacts_by_scope(task: Task, workspace: str, stage_name: str, stage_def: Optional[Dict[str, Any]] = None) -> int:
    workspace = os.path.abspath(task.workspace_path or os.path.join(WORKSPACE_ROOT, task.task_id))
    stage_type = resolve_stage_execution_profile(stage_def or {"stage_type": stage_name, "name": stage_name})
    owned_roots = _stage_cleanup_roots(stage_type)
    try:
        target = db.get_latest_stage_done_event(task.task_id, stage_name)
    except Exception:
        target = None
    if not target:
        for evt in reversed(state.history):
            if evt.task_id != task.task_id:
                continue
            if evt.event_type == "StageDone" and (evt.payload or {}).get("stage") == stage_name:
                target = evt.__dict__
                break
    if not target:
        return 0
    artifacts = (target.get("payload") or {}).get("artifacts") or []
    removed = 0
    for art in artifacts:
        uri = str((art or {}).get("uri") or "")
        if not uri or uri == "inline":
            continue
        abs_uri = os.path.abspath(uri)
        if not abs_uri.startswith(workspace):
            continue
        rel_path = os.path.relpath(abs_uri, workspace).replace("\\", "/")
        root = rel_path.split("/", 1)[0] if rel_path and rel_path != "." else ""
        if root not in owned_roots:
            continue
        try:
            if os.path.isdir(abs_uri):
                shutil.rmtree(abs_uri, ignore_errors=True)
                removed += 1
            elif os.path.exists(abs_uri):
                os.remove(abs_uri)
                removed += 1
        except Exception:
            continue
    return removed

def _should_cleanup_stage_artifacts(task: Task, stage_name: str, stage_def: Optional[Dict[str, Any]] = None) -> bool:
    stage_cfg = merged_event_configs(task).get(stage_name, {})
    if "rework_cleanup" in stage_cfg:
        return bool(stage_cfg.get("rework_cleanup"))
    return False


def _cleanup_architecture_orphan_files(task: Task, stage_name: str, stage_def: Optional[Dict[str, Any]] = None) -> int:
    return cleanup_architecture_orphan_files(task, WORKSPACE_ROOT, stage_name, stage_def)


# ---- helpers ----
REGISTRY_PROVIDER_PREFIX = "registry:model:"


def make_registry_provider_id(model_id: str) -> str:
    return f"{REGISTRY_PROVIDER_PREFIX}{model_id}"


def is_registry_provider_id(provider_id: str | None) -> bool:
    return str(provider_id or "").startswith(REGISTRY_PROVIDER_PREFIX)


def infer_provider_type(base_url: str, model_name: str = "") -> str:
    base = str(base_url or "").lower()
    model = str(model_name or "").lower()
    if "generativelanguage.googleapis.com" in base or "/v1beta" in base and "googleapis" in base:
        return "gemini"
    if "codex" in base or "codex" in model or model.startswith("gpt-5"):
        return "codex"
    if model.startswith("gemini"):
        return "gemini"
    return "openai-compatible"


def test_credential_connection(base_url: str, api_key: str) -> Dict[str, Any]:
    return _discover_models_for_credential(base_url, api_key)


def _discover_models_for_credential(base_url: str, api_key: str) -> Dict[str, Any]:
    base = str(base_url or "").rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    url = base + "/models"
    req = urllib.request.Request(
        url,
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "authorization": f"Bearer {api_key}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            models = []
            for item in payload.get("data", []) if isinstance(payload, dict) else []:
                if isinstance(item, dict) and item.get("id"):
                    models.append({
                        "name": str(item.get("id")),
                        "suggested_kind": "embedding" if "embedding" in str(item.get("id", "")).lower() else "llm",
                    })
            return {"ok": True, "models": models, "message": None}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return {"ok": False, "models": [], "message": f"HTTP {exc.code}: {exc.reason} {body[:160]}".strip()}
    except urllib.error.URLError as exc:
        return {"ok": False, "models": [], "message": f"Connection failed: {exc.reason}"}
    except Exception as exc:
        return {"ok": False, "models": [], "message": f"Error: {type(exc).__name__}: {exc}"}


def get_default_provider_id(stage_name: str | None = None, stage_type: str | None = None) -> str:
    bindings = db.get_stage_bindings()
    candidates: List[str] = []
    if stage_name:
        candidates.append(stage_name)
    normalized_type = normalize_execution_profile(stage_type or stage_name, fallback="")
    if normalized_type and normalized_type not in candidates:
        candidates.append(normalized_type)
    for candidate in candidates:
        model_id = bindings.get(candidate)
        if model_id:
            return make_registry_provider_id(model_id)
    models = [m for m in db.list_ai_models() if (m.get("model_kind") or "llm") == "llm"]
    if models:
        return make_registry_provider_id(models[0].get("model_id", ""))
    return ""


def get_registry_provider_config(provider_id: str) -> Optional[Dict[str, Any]]:
    if not is_registry_provider_id(provider_id):
        return None
    model_id = provider_id[len(REGISTRY_PROVIDER_PREFIX):]
    model_row = db.get_ai_model(model_id)
    if not model_row:
        return None
    cred = db.get_ai_credential_secret(model_row.get("credential_id", ""))
    if not cred:
        return None
    inferred_type = infer_provider_type(cred.get("base_url") or "", model_row.get("name") or "")
    provider_type = model_row.get("provider_type") or inferred_type
    if provider_type == "openai-compatible" and inferred_type == "codex":
        provider_type = "codex"
    cfg = {
        "id": provider_id,
        "type": provider_type,
        "model": model_row.get("name") or "",
        "base_url": cred.get("base_url") or "",
        "api_key_env": cred.get("api_key_env") or None,
        "api_key": cred.get("api_key") or None,
        "credential_id": cred.get("credential_id"),
        "credential_name": cred.get("name"),
        "model_id": model_row.get("model_id"),
        "model_kind": model_row.get("model_kind") or "llm",
        "source": "registry",
        "label": f"{cred.get('name') or 'Registry'} / {model_row.get('name') or ''}",
    }
    extra = model_row.get("extra_config") or {}
    if isinstance(extra, dict):
        cfg.update({k: v for k, v in extra.items() if v not in (None, "")})
    return cfg


def list_all_provider_views() -> List[Dict[str, Any]]:
    registry_items = []
    for model in db.list_ai_models():
        provider_id = make_registry_provider_id(model.get("model_id", ""))
        cfg = get_registry_provider_config(provider_id)
        if cfg:
            cfg["label"] = f"{model.get('credential_name') or 'Registry'} / {model.get('name')}"
            registry_items.append(cfg)
    return registry_items


def provider_exists(provider_id: str | None) -> bool:
    if not provider_id:
        return False
    return get_registry_provider_config(provider_id) is not None


def has_registered_llm_model() -> bool:
    return any((m.get("model_kind") or "llm") == "llm" for m in db.list_ai_models())


def require_registered_llm_model() -> None:
    if not has_registered_llm_model():
        raise HTTPException(status_code=409, detail="未配置可用的 LLM 模型，请先前往 /models.html 注册凭据、模型并绑定阶段。")


def _sse_frame(data: Dict[str, Any], *, event: str = "update") -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def normalize_stage_definition(stage_def: Dict[str, Any]) -> Dict[str, Any]:
    raw_name = str(stage_def.get("name") or stage_def.get("id") or stage_def.get("label") or "").strip()
    execution_profile = resolve_stage_execution_profile(
        {
            "execution_profile": stage_def.get("execution_profile"),
            "stage_type": stage_def.get("stage_type") or raw_name,
            "stage_semantics": stage_def.get("stage_semantics") or stage_def.get("semantic_type"),
            "label": stage_def.get("label"),
            "name": raw_name,
        }
    )
    reference = dict(WORKFLOW_STAGE_MAP.get(execution_profile) or {})
    blueprint = dict(build_stage_type_blueprints(CAPA_CONFIG, SKILL_CONFIG).get(execution_profile) or STAGE_TYPE_BLUEPRINTS.get(execution_profile) or {})
    stage_semantics = resolve_stage_semantics(stage_def, execution_profile=execution_profile)
    normalized = {
        "name": raw_name or execution_profile,
        "stage_type": execution_profile,
        "execution_profile": execution_profile,
        "stage_semantics": stage_semantics,
        "label": stage_def.get("label") or blueprint.get("label") or reference.get("label") or raw_name or execution_profile,
        "role": stage_def.get("role") or blueprint.get("role") or "",
        "skills": stage_def.get("skills") or reference.get("skills") or blueprint.get("skills") or [],
        "capabilities": stage_def.get("capabilities") or reference.get("capabilities") or blueprint.get("capabilities") or [],
        "human_checkpoint": bool(stage_def.get("human_checkpoint", reference.get("human_checkpoint", blueprint.get("human_checkpoint", False)))),
        "depends_on": [str(dep) for dep in stage_def.get("depends_on", []) if dep] if isinstance(stage_def.get("depends_on"), list) else [],
    }
    if stage_def.get("conversation_group"):
        normalized["conversation_group"] = stage_def.get("conversation_group")
    if stage_def.get("prompt_template"):
        normalized["prompt_template"] = stage_def.get("prompt_template")
    if stage_def.get("acceptance_criteria"):
        normalized["acceptance_criteria"] = stage_def.get("acceptance_criteria")
    return normalized



def task_stage_definitions(task: Optional[Task] = None) -> List[Dict[str, Any]]:
    if not task:
        return [normalize_stage_definition(stage) for stage in WORKFLOW_TEMPLATE.get("stages", [])]
    context = task.context or {}
    leader_plan = context.get("leader_plan") or {}
    raw_plan = leader_plan.get("stages") if isinstance(leader_plan, dict) else None
    stage_defs = [normalize_stage_definition(stage) for stage in raw_plan if isinstance(stage, dict)] if isinstance(raw_plan, list) and raw_plan else []
    if not stage_defs:
        stage_defs = [normalize_stage_definition(stage) for stage in WORKFLOW_TEMPLATE.get("stages", [])]
    known_names = {stage["name"] for stage in stage_defs}
    event_configs = context.get("event_configs") or {}
    if isinstance(raw_plan, list) and raw_plan:
        for stage_name, cfg in event_configs.items():
            if stage_name in known_names or stage_name in EXECUTION_PROFILE_KEYS or stage_name == "planning":
                continue
            stage_defs.append(normalize_stage_definition({
                "name": stage_name,
                "execution_profile": (cfg or {}).get("execution_profile"),
                "stage_type": (cfg or {}).get("stage_type") or stage_name,
                "stage_semantics": (cfg or {}).get("stage_semantics"),
            }))
    return stage_defs



def task_stage_sequence(task: Optional[Task] = None) -> List[str]:
    return [stage["name"] for stage in task_stage_definitions(task)]



def task_stage_map(task: Optional[Task] = None) -> Dict[str, Dict[str, Any]]:
    return {stage["name"]: stage for stage in task_stage_definitions(task)}


def task_conversation_groups(task: Optional[Task] = None) -> List[Dict[str, Any]]:
    if not task:
        return resolve_conversation_groups(task_stage_definitions(None), None)
    context = task.context or {}
    leader_plan = context.get("leader_plan") or {}
    raw_groups = leader_plan.get("conversation_groups") if isinstance(leader_plan, dict) else None
    return resolve_conversation_groups(task_stage_definitions(task), raw_groups)


def task_conversation_group_map(task: Optional[Task] = None) -> Dict[str, Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    for group in task_conversation_groups(task):
        for stage_name in group.get("stage_names", []) or []:
            mapping[str(stage_name)] = group
    return mapping


def build_task_group_blackboards(
    task: Task,
    *,
    messages: Optional[List[Dict[str, Any]]] = None,
    blackboard: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    stage_map = task_stage_map(task)
    group_defs = task_conversation_groups(task)
    group_map_by_stage = task_conversation_group_map(task)
    messages = messages if messages is not None else db.list_conversation_messages(task.task_id, limit=200)
    blackboard = blackboard if blackboard is not None else db.list_blackboard_entries(task.task_id, limit=200)

    grouped: Dict[str, Dict[str, Any]] = {}

    def ensure_group(key: str, label: str, stage_names: List[str]) -> Dict[str, Any]:
        if key in grouped:
            return grouped[key]
        grouped[key] = {
            "key": key,
            "label": label or key or "全局协作",
            "stage_names": [name for name in (stage_names or []) if name in stage_map],
            "entries": [],
            "messages": [],
        }
        return grouped[key]

    for group in group_defs:
        ensure_group(
            str(group.get("key") or ""),
            str(group.get("label") or group.get("key") or "协作分组"),
            [str(name) for name in (group.get("stage_names") or [])],
        )

    def resolve_group(stage_name: str | None, explicit_key: str | None = None, explicit_label: str | None = None) -> Dict[str, Any]:
        if explicit_key:
            existing = next((group for group in group_defs if str(group.get("key") or "") == explicit_key), None)
            if existing:
                return ensure_group(
                    str(existing.get("key") or explicit_key),
                    str(existing.get("label") or explicit_label or explicit_key),
                    [str(name) for name in (existing.get("stage_names") or [])],
                )
            return ensure_group(str(explicit_key), str(explicit_label or explicit_key), [str(stage_name)] if stage_name else [])
        if stage_name and stage_name in group_map_by_stage:
            group = group_map_by_stage[stage_name]
            return ensure_group(
                str(group.get("key") or stage_name),
                str(group.get("label") or stage_name),
                [str(name) for name in (group.get("stage_names") or [])],
            )
        stage = stage_map.get(str(stage_name or ""))
        fallback_key = str(stage_name or "__global__")
        fallback_label = str((stage or {}).get("label") or stage_name or "全局协作")
        return ensure_group(fallback_key, fallback_label, [str(stage_name)] if stage_name else [])

    for message in messages:
        group = resolve_group(
            str(message.get("stage_name") or ""),
            str(message.get("group_key") or "") or None,
            str(message.get("group_label") or "") or None,
        )
        group["messages"].append(message)

    for entry in blackboard:
        group = resolve_group(
            str(entry.get("stage_name") or ""),
            str(entry.get("group_key") or "") or None,
            str(entry.get("group_label") or "") or None,
        )
        group["entries"].append(entry)

    result: List[Dict[str, Any]] = []
    for group in grouped.values():
        if not group["entries"] and not group["messages"]:
            continue
        snapshot = build_blackboard_snapshot(group["entries"], fallback_title=f"{group['label']} 共享黑板")
        if not snapshot.get("shared_context") and group["messages"]:
            latest_message = sorted(group["messages"], key=lambda item: float(item.get("created_at") or 0), reverse=True)[0]
            snapshot["shared_context"] = str(latest_message.get("content") or "").strip()[:240]
            snapshot["latest_update"] = snapshot["shared_context"]
            snapshot["updated_at"] = latest_message.get("created_at")
        result.append({
            "key": group["key"],
            "label": group["label"],
            "stage_names": group["stage_names"],
            "entry_count": len(group["entries"]),
            "message_count": len(group["messages"]),
            **snapshot,
        })

    result.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    return result



def resolve_task_stage_name(task: Task, stage_name: str) -> str:
    stage_name = str(stage_name or "").strip()
    stage_map = task_stage_map(task)
    if stage_name in stage_map:
        return stage_name
    normalized = normalize_execution_profile(stage_name, fallback="")
    matches = [stage["name"] for stage in task_stage_definitions(task) if stage.get("execution_profile") == normalized or stage.get("stage_type") == normalized]
    return matches[0] if matches else stage_name



def get_stage_runtime_defaults(stage_name: str, stage_type: str | None = None) -> Dict[str, Any]:
    normalized = normalize_execution_profile(stage_type or stage_name)
    return dict(STAGE_RUNTIME_DEFAULTS.get(normalized, {}))



def ensure_task_defaults(task: Task) -> bool:
    context = task.context or {}
    changed = False
    default_provider = context.get("default_model_provider")
    if default_provider and not provider_exists(default_provider):
        context["default_model_provider"] = None
        default_provider = None
        changed = True
    if not default_provider:
        context["default_model_provider"] = get_default_provider_id()
        default_provider = context.get("default_model_provider")
        changed = True
    event_configs = context.get("event_configs")
    if not isinstance(event_configs, dict):
        context["event_configs"] = {}
        event_configs = context["event_configs"]
        changed = True

    stage_defs = task_stage_definitions(task)
    stage_map = {stage["name"]: stage for stage in stage_defs}
    all_stage_keys = set(EXECUTION_PROFILE_KEYS)
    all_stage_keys.update(stage_map.keys())
    all_stage_keys.update(key for key in event_configs.keys() if key != "planning")

    for stage in all_stage_keys:
        raw_cfg = event_configs.get(stage) or {}
        stage_type = resolve_stage_execution_profile({
            "execution_profile": raw_cfg.get("execution_profile") or stage_map.get(stage, {}).get("execution_profile"),
            "stage_type": raw_cfg.get("stage_type") or stage_map.get(stage, {}).get("stage_type") or stage,
            "stage_semantics": raw_cfg.get("stage_semantics") or stage_map.get(stage, {}).get("stage_semantics"),
            "name": stage,
        })
        stage_cfg = dict(raw_cfg)
        if stage_cfg.get("model_provider") and not provider_exists(stage_cfg.get("model_provider")):
            stage_cfg.pop("model_provider", None)
            changed = True
        if stage_cfg.get("stage_type") != stage_type:
            stage_cfg["stage_type"] = stage_type
            changed = True
        if stage_cfg.get("execution_profile") != stage_type:
            stage_cfg["execution_profile"] = stage_type
            changed = True
        semantics = resolve_stage_semantics(
            {
                "stage_semantics": stage_cfg.get("stage_semantics") or stage_map.get(stage, {}).get("stage_semantics"),
                "label": stage_map.get(stage, {}).get("label") or stage,
                "stage_type": stage_type,
            },
            execution_profile=stage_type,
        )
        if stage_cfg.get("stage_semantics") != semantics:
            stage_cfg["stage_semantics"] = semantics
            changed = True
        defaults = get_stage_runtime_defaults(stage, stage_type)
        for key, value in defaults.items():
            if key not in stage_cfg:
                stage_cfg[key] = value
                changed = True
        if not stage_cfg.get("model_provider"):
            stage_default = get_default_provider_id(stage_name=stage, stage_type=stage_type) or default_provider
            if stage_default:
                stage_cfg["model_provider"] = stage_default
                changed = True
        event_configs[stage] = stage_cfg

    task.context = context
    return changed



def merged_event_configs(task: Task) -> Dict[str, Dict[str, Any]]:
    raw = task.context.get("event_configs") or {}
    task_default = task.context.get("default_model_provider") or get_default_provider_id()
    effective: Dict[str, Dict[str, Any]] = {}
    for stage in task_stage_definitions(task):
        stage_name = stage.get("name") or ""
        stage_type = resolve_stage_execution_profile(stage)
        cfg = get_stage_runtime_defaults(stage_name, stage_type)
        if stage_type != stage_name:
            cfg.update(raw.get(stage_type, {}))
        cfg.update(raw.get(stage_name, {}))
        cfg["stage_type"] = stage_type
        cfg["execution_profile"] = stage_type
        cfg["stage_semantics"] = resolve_stage_semantics(stage, execution_profile=stage_type)
        if not cfg.get("model_provider"):
            cfg["model_provider"] = get_default_provider_id(stage_name=stage_name, stage_type=stage_type) or task_default
        effective[stage_name] = cfg
    return effective



def stage_details(task: Task) -> Dict[str, Dict[str, Any]]:
    effective_cfg = merged_event_configs(task)
    conversation_group_map = task_conversation_group_map(task)
    spec = (task.context or {}).get("spec", "")
    details: Dict[str, Dict[str, Any]] = {}
    for stage in task_stage_definitions(task):
        stage_name = stage.get("name") or ""
        stage_type = resolve_stage_execution_profile(stage)
        stage_semantics = resolve_stage_semantics(stage, execution_profile=stage_type)
        cfg = effective_cfg.get(stage_name, {})
        prompt_override = cfg.get("prompt_template")
        prompt_key = stage_type if stage_type in DEFAULT_STAGE_PROMPTS else stage_name
        default_prompt = DEFAULT_STAGE_PROMPTS.get(prompt_key, "")
        effective_prompt = render_stage_prompt(stage_name, spec, prompt_override if isinstance(prompt_override, str) else None, stage_type=stage_type, execution_profile=stage_type)
        details[stage_name] = {
            "label": stage.get("label", stage_name),
            "stage_type": stage_type,
            "execution_profile": stage_type,
            "stage_semantics": stage_semantics,
            "role": stage.get("role") or cfg.get("planned_role") or "",
            "skills": stage.get("skills", []),
            "capabilities": stage.get("capabilities", []),
            "depends_on": stage.get("depends_on", []),
            "human_checkpoint": bool(stage.get("human_checkpoint", False)),
            "conversation_group": conversation_group_map.get(stage_name) or stage.get("conversation_group"),
            "default_prompt_template": default_prompt,
            "effective_prompt": effective_prompt,
            "prompt_overridden": bool(prompt_override),
            "runtime_defaults": get_stage_runtime_defaults(stage_name, stage_type),
            "acceptance_criteria": cfg.get("acceptance_criteria") or stage.get("acceptance_criteria") or "",
        }
    return details



def infer_resume_stage(task: Task) -> Optional[str]:
    stage_sequence = task_stage_sequence(task)
    if not stage_sequence:
        return None
    try:
        events = db.get_events(task.task_id, limit=1000)
    except Exception:
        events = [e.__dict__ for e in state.history if e.task_id == task.task_id][-1000:]
    if not events:
        return None
    latest_by_stage: Dict[str, Dict[str, Any]] = {}
    for evt in events:
        payload = evt.get("payload") or {}
        stage = payload.get("stage")
        if stage in stage_sequence:
            latest_by_stage[stage] = evt
    for stage in stage_sequence:
        evt = latest_by_stage.get(stage)
        if not evt:
            return stage
        event_type = evt.get("event_type")
        payload = evt.get("payload") or {}
        if event_type == "StageReview" and payload.get("pass") is False:
            return stage
        if event_type in {"StageError", "StageAbort", "StageAwait", "StageRework", "StageRerunError", "StageRerunAborted", "StageRerunFailed"}:
            return stage
        if event_type == "StageStart":
            return stage
    for stage in reversed(stage_sequence):
        evt = latest_by_stage.get(stage)
        payload = (evt or {}).get("payload") or {}
        if evt and (
            evt.get("event_type") in {"StageDone", "StageRerunDone"}
            or (evt.get("event_type") == "StageReview" and payload.get("pass") is True)
        ):
            idx = stage_sequence.index(stage)
            if idx + 1 < len(stage_sequence):
                return stage_sequence[idx + 1]
            return None
    return None


def latest_stage_runtime_outcome(task_id: str, stage_name: str) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    try:
        events.extend(db.get_events(task_id, limit=1000))
    except Exception:
        pass
    for event in state.history:
        if getattr(event, "task_id", None) != task_id:
            continue
        events.append({
            "event_id": getattr(event, "event_id", ""),
            "task_id": getattr(event, "task_id", ""),
            "event_type": getattr(event, "event_type", ""),
            "payload": getattr(event, "payload", None) or {},
            "timestamp": float(getattr(event, "timestamp", 0) or 0),
        })
    deduped: Dict[str, Dict[str, Any]] = {}
    for event in events:
        event_id = str(event.get("event_id") or "")
        if event_id:
            deduped[event_id] = event
        else:
            deduped[f"{event.get('event_type')}::{event.get('timestamp')}::{len(deduped)}"] = event
    ordered = sorted(deduped.values(), key=lambda item: float(item.get("timestamp") or 0))
    for event in reversed(ordered):
        payload = event.get("payload") or {}
        if payload.get("stage") != stage_name:
            continue
        event_type = str(event.get("event_type") or "")
        if event_type == "StageAwait":
            return {"status": "waiting_user", "event_type": event_type, "payload": payload}
        if event_type in {"StageError", "StageRerunError", "StageRerunFailed"}:
            return {
                "status": "failed",
                "event_type": event_type,
                "payload": payload,
                "feedback": payload.get("feedback") or payload.get("error") or "",
            }
        if event_type == "StageReview":
            if payload.get("pass") is False:
                return {
                    "status": "failed",
                    "event_type": event_type,
                    "payload": payload,
                    "feedback": payload.get("feedback") or "",
                }
            if payload.get("pass") is True:
                return {"status": "ok", "event_type": event_type, "payload": payload}
        if event_type in {"StageDone", "StageRerunDone"}:
            return {"status": "ok", "event_type": event_type, "payload": payload}
    return {"status": "ok", "event_type": "", "payload": {}}


def ensure_task(task_id: str) -> Task:
    task = state.tasks.get(task_id)
    if task:
        return task
    try:
        row = db.get_task(task_id)
        if row:
            restored = Task(
                task_id=row["task_id"],
                domain=row.get("domain", "software"),
                required_capabilities=row.get("required_capabilities") or [],
                context=row.get("context") or {},
                priority=int(row.get("priority") or 50),
                workspace_path=row.get("workspace_path"),
            )
            state.tasks[task_id] = restored
            state.task_status[task_id] = row.get("status", "created")
            if ensure_task_defaults(restored):
                db.update_task_context(restored.task_id, restored.context)
            return restored
    except Exception:
        pass
    raise HTTPException(status_code=404, detail="task not found")


def run_step(task: Task):
    """顺序执行一次任务工作流。"""
    pending_decision = _pending_human_decision(task)
    if pending_decision:
        _set_task_status(task.task_id, "waiting_user")
        return {"status": "await_user", "await": pending_decision}
    clear_task_abort(task.task_id)
    if ensure_task_defaults(task):
        db.update_task_context(task.task_id, task.context)

    leader_plan = (task.context or {}).get("leader_plan") or {}
    if leader_plan:
        write_leader_plan_snapshot(task, WORKSPACE_ROOT, leader_plan)
        resume_stage = infer_resume_stage(task)
        if resume_stage:
            return run_single_stage(task, resume_stage)
        return _complete_task(task)

    _set_task_status(task.task_id, "running")

    plan = graph_builder.plan_workflow(task, WORKFLOW_TEMPLATE)
    ensure_task_defaults(task)
    tpl = {"stages": plan.get("stages", WORKFLOW_TEMPLATE.get("stages", []))}
    plan_evt = new_event("leader", task.task_id, "PlanGenerated", {
        "stages": plan.get("stages", []),
        "leader_plan": task.context.get("leader_plan", {}),
        "conversation_groups": (task.context.get("leader_plan", {}) or {}).get("conversation_groups", []),
    })
    if not is_task_aborted(task.task_id):
        _record_event(plan_evt)
    db.update_task_context(task.task_id, task.context)

    try:
        result = workflow_runner.invoke(task, tpl)
        if result.get("status") == "deleted":
            return result
        if result.get("error"):
            workflow_runner.record_graph_error(task, str(result.get("error")))
            _set_task_status(task.task_id, 'failed')
            return {'status': 'failed', 'error': str(result.get("error"))}
        if is_task_aborted(task.task_id) or result.get("abort"):
            workflow_runner.record_graph_abort(task, result.get('abort') or {'reason': 'task_aborted'})
            _set_task_status(task.task_id, 'aborted')
            return {'status': 'aborted', 'abort': result.get('abort')}
        workflow_runner.record_graph_run(task, result)
        workflow_runner.persist_context(task)
    except Exception as e:
        if task.task_id not in state.tasks:
            return {"status": "deleted", "task_id": task.task_id}
        if is_task_aborted(task.task_id):
            _set_task_status(task.task_id, 'aborted')
            return {'status': 'aborted', 'abort': {'reason': 'task_aborted', 'error': str(e)}}
        workflow_runner.record_graph_error(task, str(e))
        _set_task_status(task.task_id, 'failed')
        return {'status': 'failed', 'error': str(e)}

    if result.get('await'):
        _set_task_status(task.task_id, 'waiting_user')
        return {'status': 'await_user', 'await': result['await']}

    return _complete_task(task, artifacts=result.get('artifacts', []))


def run_single_stage(task: Task, stage_name: str):
    pending_decision = _pending_human_decision(task)
    if pending_decision:
        _set_task_status(task.task_id, "waiting_user")
        return {"status": "await_user", "stage": pending_decision.get("stage"), "await": pending_decision}
    resolved_stage = resolve_task_stage_name(task, stage_name)
    stage_map = task_stage_map(task)
    if resolved_stage not in stage_map:
        if stage_name in EXECUTION_PROFILE_KEYS:
            stage_def = normalize_stage_definition(WORKFLOW_STAGE_MAP.get(stage_name) or {"name": stage_name, "stage_type": stage_name})
            resolved_stage = stage_def["name"]
        else:
            raise HTTPException(status_code=400, detail="unsupported stage")
    else:
        stage_def = stage_map[resolved_stage]

    clear_task_abort(task.task_id)
    if ensure_task_defaults(task):
        db.update_task_context(task.task_id, task.context)
    _set_task_status(task.task_id, "running")

    tpl = {"stages": [stage_def]}
    cleanup_enabled = _should_cleanup_stage_artifacts(task, resolved_stage, stage_def)
    removed_count = _cleanup_latest_stage_artifacts(task, resolved_stage) if cleanup_enabled else 0
    orphan_removed = _cleanup_architecture_orphan_files(task, resolved_stage, stage_def)
    req_evt = new_event("user", task.task_id, "StageRerunRequested", {
        "stage": resolved_stage,
        "stage_type": stage_def.get("stage_type"),
        "execution_profile": stage_def.get("execution_profile") or stage_def.get("stage_type"),
        "stage_semantics": stage_def.get("stage_semantics"),
        "label": stage_def.get("label", resolved_stage),
        "cleanup_enabled": cleanup_enabled,
        "cleaned_artifacts": removed_count,
        "cleaned_orphans": orphan_removed,
    })
    if not is_task_aborted(task.task_id):
        _record_event(req_evt)

    try:
        result = workflow_runner.invoke(task, tpl)
        if result.get("status") == "deleted":
            return {"status": "deleted", "stage": resolved_stage, "task_id": task.task_id}
        if result.get("error"):
            err_evt = new_event("user", task.task_id, "StageRerunError", {"stage": resolved_stage, "error": str(result.get("error"))})
            _record_event(err_evt)
            _set_task_status(task.task_id, "failed")
            return {"status": "failed", "stage": resolved_stage, "error": str(result.get("error"))}
        if is_task_aborted(task.task_id) or result.get("abort"):
            abort_evt = new_event("user", task.task_id, "StageRerunAborted", {"stage": resolved_stage, "abort": result.get("abort") or {"reason": "task_aborted"}})
            _record_event(abort_evt)
            _set_task_status(task.task_id, 'aborted')
            return {"status": "aborted", "stage": resolved_stage, "abort": result.get("abort")}
        stage_outcome = latest_stage_runtime_outcome(task.task_id, resolved_stage)
        if stage_outcome.get("status") == "failed":
            fail_evt = new_event("user", task.task_id, "StageRerunFailed", {
                "stage": resolved_stage,
                "stage_type": stage_def.get("stage_type"),
                "error": stage_outcome.get("payload", {}).get("error") or "",
                "feedback": stage_outcome.get("feedback") or "",
            })
            _record_event(fail_evt)
            _set_task_status(task.task_id, "failed")
            return {
                "status": "failed",
                "stage": resolved_stage,
                "error": fail_evt.payload.get("error") or fail_evt.payload.get("feedback") or "stage_review_failed",
            }
        done_evt = new_event("user", task.task_id, "StageRerunDone", {
            "stage": resolved_stage,
            "stage_type": stage_def.get("stage_type"),
            "await": result.get("await"),
            "artifact_count": len(_latest_stage_artifacts(task.task_id, resolved_stage)),
        })
        _record_event(done_evt)
        workflow_runner.persist_context(task)
        if result.get("await") or stage_outcome.get("status") == "waiting_user":
            _set_task_status(task.task_id, "waiting_user")
            return {"status": "await_user", "stage": resolved_stage, "await": result.get("await")}
        next_stage = infer_resume_stage(task)
        next_status = "completed" if next_stage is None else "created"
        _set_task_status(task.task_id, next_status)
    except Exception as e:
        err_evt = new_event("user", task.task_id, "StageRerunError", {"stage": resolved_stage, "error": str(e)})
        _record_event(err_evt)
        _set_task_status(task.task_id, "failed")
        return {"status": "failed", "stage": resolved_stage, "error": str(e)}

    return {
        "status": "ok",
        "stage": resolved_stage,
        "await": result.get("await"),
        "artifacts": _latest_stage_artifacts(task.task_id, resolved_stage),
        "task_status": state.task_status.get(task.task_id, "created"),
    }


# ---- API 路由 ----
@app.get("/tasks")
def list_tasks():
    # 优先返回数据库持久化任务；数据库不可用时退回内存态。
    try:
        tasks = db.list_tasks()
        if tasks:
            merged = []
            for row in tasks:
                item = dict(row)
                runtime_status = state.task_status.get(item.get("task_id", ""))
                item["status"] = _merge_presented_task_status(item.get("status", ""), runtime_status or "")
                merged.append(item)
            return merged
    except Exception:
        pass
    return [
        {
            "task_id": tid,
            "status": state.task_status.get(tid, "unknown"),
            "required_capabilities": task.required_capabilities,
            "context": task.context,
            "workspace_path": task.workspace_path,
        }
        for tid, task in state.tasks.items()
    ]


@app.post("/tasks", response_model=TaskWorkspaceResponse)
def create_task(body: CreateTaskRequest):
    require_registered_llm_model()
    return task_app_service.create_task(_payload_dict(body))


@app.post("/tasks/{task_id}/step", response_model=TaskStatusResponse)
def step_task(task_id: str):
    require_registered_llm_model()
    return task_app_service.step_task(task_id)


@app.post("/tasks/{task_id}/stages/{stage_name}/rerun", response_model=TaskStatusResponse)
def rerun_stage(task_id: str, stage_name: str):
    require_registered_llm_model()
    return task_app_service.rerun_stage(task_id, stage_name)


@app.post("/tasks/{task_id}/abort", response_model=TaskStatusResponse)
def abort_task(task_id: str):
    return task_app_service.abort_task(task_id)


@app.delete("/tasks/{task_id}", response_model=TaskStatusResponse)
def delete_task(task_id: str, purge_workspace: bool = True):
    task = ensure_task(task_id)
    runtime.drop_task(task_id)
    workspace_path = task.workspace_path
    db.delete_task(task_id)

    removed_workspace = False
    if purge_workspace and workspace_path and os.path.exists(workspace_path):
        shutil.rmtree(workspace_path, ignore_errors=True)
        removed_workspace = True
    return {"status": "ok", "task_id": task_id, "workspace_removed": removed_workspace}


@app.post("/tasks/{task_id}/model", response_model=SetTaskModelResponse)
def set_task_model(task_id: str, body: SetTaskModelRequest):
    require_registered_llm_model()
    task = ensure_task(task_id)
    payload = _payload_dict(body)
    provider = payload.get("model_provider")
    if provider and not provider_exists(provider):
        raise HTTPException(status_code=400, detail="provider not found")
    task.context["default_model_provider"] = provider
    task.context.pop("model_provider", None)
    ensure_task_defaults(task)
    db.update_task_context(task.task_id, task.context)
    evt = new_event("user", task_id, "TaskModelUpdated", {"model_provider": provider})
    _record_event(evt)
    return {"status": "ok", "model_provider": provider}


@app.get("/tasks/{task_id}/event-configs")
def get_task_event_configs(task_id: str):
    task = ensure_task(task_id)
    event_configs = task.context.get("event_configs") or {}
    effective = merged_event_configs(task)
    stage_defs = task_stage_definitions(task)
    conversation_groups = task_conversation_groups(task)
    return {
        "task_id": task_id,
        "stages": [stage.get("name") for stage in stage_defs],
        "stage_definitions": stage_defs,
        "conversation_groups": conversation_groups,
        "reference_stage_types": EXECUTION_PROFILE_KEYS,
        "reference_execution_profiles": EXECUTION_PROFILE_KEYS,
        "reference_stage_semantics": sorted({"analysis", "planning", "design", "creation", "transformation", "verification", "delivery", "decision", "coordination"}),
        "default_model_provider": task.context.get("default_model_provider") or get_default_provider_id(),
        "event_configs": event_configs,
        "effective_event_configs": effective,
        "event_details": stage_details(task),
        "stage_bindings": db.get_stage_bindings(),
        "leader_plan": (task.context or {}).get("leader_plan") or {},
    }


@app.get("/tasks/{task_id}/collaboration")
def get_task_collaboration(task_id: str, stage_name: Optional[str] = None, conversation_id: Optional[str] = None, limit: int = 200):
    task = ensure_task(task_id)
    conversation_group_map = task_conversation_group_map(task)
    conversation_groups = task_conversation_groups(task)
    messages = db.list_conversation_messages(task_id, stage_name=stage_name, conversation_id=conversation_id, limit=limit)
    for message in messages:
        group = conversation_group_map.get(str(message.get("stage_name") or ""))
        if group:
            message["group_key"] = group.get("key")
            message["group_label"] = group.get("label")
    blackboard = db.list_blackboard_entries(task_id, stage_name=stage_name, limit=limit)
    for entry in blackboard:
        group = conversation_group_map.get(str(entry.get("stage_name") or ""))
        if group:
            entry["group_key"] = group.get("key")
            entry["group_label"] = group.get("label")
    group_blackboards = build_task_group_blackboards(task, messages=messages, blackboard=blackboard)
    return {
        "task_id": task_id,
        "conversation_groups": conversation_groups,
        "group_blackboards": group_blackboards,
        "pending_human_decision": _pending_human_decision(task),
        "messages": messages,
        "blackboard": blackboard,
    }


def _submit_human_decision_impl(task_id: str, body: Dict[str, Any]):
    task = ensure_task(task_id)
    pending = _pending_human_decision(task)
    if not pending:
        raise HTTPException(status_code=409, detail="当前没有待处理的人工决策")

    decision_text = str(body.get("decision") or body.get("message") or "").strip()
    selected_option = str(body.get("selected_option") or "").strip()
    if not decision_text and not selected_option:
        raise HTTPException(status_code=400, detail="请至少提供决策内容或选择一个方案")

    stage_name = str(pending.get("stage") or "")
    stage_map = task_stage_map(task)
    stage_def = stage_map.get(stage_name, {})
    stage_type = resolve_stage_execution_profile(
        {
            "execution_profile": (stage_def or {}).get("execution_profile") or pending.get("execution_profile"),
            "stage_type": (stage_def or {}).get("stage_type") or pending.get("stage_type") or stage_name,
            "stage_semantics": (stage_def or {}).get("stage_semantics") or pending.get("stage_semantics"),
            "label": (stage_def or {}).get("label") or pending.get("label"),
            "name": stage_name,
        }
    )
    stage_semantics = resolve_stage_semantics(stage_def or pending, execution_profile=stage_type)
    stage_label = str((stage_def or {}).get("label") or pending.get("label") or stage_name or "当前阶段")
    decision_lines = []
    if selected_option:
        decision_lines.append(f"用户选择：{selected_option}")
    if decision_text:
        decision_lines.append(f"用户意见：{decision_text}")
    decision_lines.append(f"对应问题：{str(pending.get('question') or '').strip()}")
    if pending.get("why_blocked"):
        decision_lines.append(f"决策原因：{str(pending.get('why_blocked') or '').strip()}")
    final_text = "\n".join(line for line in decision_lines if line).strip()

    collaboration = CollaborationHub(task)
    conversation_id = collaboration.ensure_thread(
        stage_name,
        stage_type=stage_type,
        thread_kind="human_decision",
        title=f"{stage_label} 人工决策",
        participants=[
            {"actor_id": "leader-review", "role": "Leader/评审"},
            {"actor_id": "user", "role": "人工决策"},
        ],
    )
    message = collaboration.post_message(
        stage_name=stage_name,
        stage_type=stage_type,
        actor_id="user",
        actor_role="人工决策",
        content=final_text,
        message_type="user_decision",
        conversation_id=conversation_id,
        thread_kind="human_decision",
        recipient_id="leader-review",
        payload={
            "question": pending.get("question"),
            "selected_option": selected_option,
            "decision": decision_text,
        },
    )
    collaboration.upsert_blackboard(
        entry_key=f"stage:{stage_name}:human_decision_request",
        title=f"{stage_label} 待人工决策",
        content=f"已收到人工决策：{selected_option or decision_text or '已填写'}",
        entry_type="human_decision_request",
        stage_name=stage_name,
        payload={**pending, "resolved": True, "selected_option": selected_option},
        source_message_id=message.get("message_id"),
    )
    collaboration.upsert_blackboard(
        entry_key=f"stage:{stage_name}:user_decision",
        title=f"{stage_label} 人工决策",
        content=final_text,
        entry_type="user_decision",
        stage_name=stage_name,
        payload={
            "question": pending.get("question"),
            "selected_option": selected_option,
            "decision": decision_text,
            "resolved": True,
        },
        source_message_id=message.get("message_id"),
    )
    history = task.context.setdefault("human_decision_history", []) if isinstance(task.context, dict) else []
    if isinstance(history, list):
        history.append({
            **pending,
            "selected_option": selected_option,
            "decision": decision_text,
            "submitted_at": message.get("created_at"),
        })
        if len(history) > 20:
            del history[:-20]
    _clear_pending_human_decision(task)
    db.update_task_context(task.task_id, task.context)
    evt = new_event("user", task.task_id, "HumanDecisionSubmitted", {
        "stage": stage_name,
        "stage_type": stage_type,
        "execution_profile": stage_type,
        "stage_semantics": stage_semantics,
        "label": stage_label,
        "question": pending.get("question"),
        "selected_option": selected_option,
        "decision": decision_text,
    })
    _record_event(evt)
    _set_task_status(task.task_id, "created")
    return {"status": "ok", "stage": stage_name, "message": "人工决策已记录，请继续执行 Step"}


task_app_service = TaskApplicationService(
    create_task_fn=_create_task_impl,
    run_step_fn=run_step,
    run_single_stage_fn=run_single_stage,
    abort_task_fn=_abort_task_impl,
    submit_human_decision_fn=_submit_human_decision_impl,
    ensure_task_fn=ensure_task,
)


@app.post("/tasks/{task_id}/human-decisions", response_model=TaskStatusResponse)
def submit_human_decision(task_id: str, body: SubmitHumanDecisionRequest):
    return task_app_service.submit_human_decision(task_id, _payload_dict(body))


@app.get("/tasks/{task_id}/conversations")
def get_task_conversations(task_id: str, stage_name: Optional[str] = None, conversation_id: Optional[str] = None, limit: int = 200):
    ensure_task(task_id)
    return db.list_conversation_messages(task_id, stage_name=stage_name, conversation_id=conversation_id, limit=limit)


@app.get("/tasks/{task_id}/blackboard")
def get_task_blackboard(task_id: str, stage_name: Optional[str] = None, limit: int = 200):
    ensure_task(task_id)
    return db.list_blackboard_entries(task_id, stage_name=stage_name, limit=limit)


@app.put("/tasks/{task_id}/event-configs/{event_name}", response_model=UpdateTaskEventConfigResponse)
def update_task_event_config(task_id: str, event_name: str, body: UpdateTaskEventConfigRequest):
    require_registered_llm_model()
    task = ensure_task(task_id)
    payload = _payload_dict(body)
    allowed_names = set(task_stage_sequence(task)) | set(EXECUTION_PROFILE_KEYS)
    target_name = resolve_task_stage_name(task, event_name)
    if target_name not in allowed_names and event_name not in allowed_names:
        raise HTTPException(status_code=400, detail="unsupported event/stage")
    if target_name not in allowed_names:
        target_name = event_name

    model_provider = payload.get("model_provider")
    if model_provider and not provider_exists(model_provider):
        raise HTTPException(status_code=400, detail="provider not found")

    stage_def = task_stage_map(task).get(target_name)
    stage_type = resolve_stage_execution_profile(
        {
            "execution_profile": (stage_def or {}).get("execution_profile") or (task.context.get("event_configs") or {}).get(target_name, {}).get("execution_profile"),
            "stage_type": (stage_def or {}).get("stage_type") or (task.context.get("event_configs") or {}).get(target_name, {}).get("stage_type") or target_name,
            "stage_semantics": (stage_def or {}).get("stage_semantics") or (task.context.get("event_configs") or {}).get(target_name, {}).get("stage_semantics"),
            "label": (stage_def or {}).get("label") or target_name,
            "name": target_name,
        }
    )
    stage_semantics = resolve_stage_semantics(stage_def or {"name": target_name, "stage_type": stage_type}, execution_profile=stage_type)
    event_configs = task.context.setdefault("event_configs", {})
    prev_cfg = event_configs.get(target_name, {})
    next_cfg = dict(prev_cfg)

    for key in [
        "model_provider", "model", "temperature", "timeout", "base_url", "api_key_env", "api_key",
        "notes", "prompt_template", "test_command", "full_test_command", "smoke_test_command", "auto_fix_limit",
        "auto_smoke_fix_limit", "auto_rework_limit", "review_blocking", "smoke_test_blocking",
        "rework_cleanup", "targeted_rework_enabled", "acceptance_criteria", "planned_role",
    ]:
        if key in payload:
            next_cfg[key] = payload.get(key)

    next_cfg["stage_type"] = stage_type
    next_cfg["execution_profile"] = stage_type
    next_cfg["stage_semantics"] = stage_semantics
    compact_cfg = {k: v for k, v in next_cfg.items() if v not in ("", None)}
    event_configs[target_name] = compact_cfg
    task.context["event_configs"] = event_configs
    task.context.setdefault("default_model_provider", get_default_provider_id())
    ensure_task_defaults(task)
    db.update_task_context(task.task_id, task.context)

    evt = new_event("user", task_id, "TaskEventConfigUpdated", {"event": target_name, "stage_type": stage_type, "execution_profile": stage_type, "stage_semantics": stage_semantics, "config": compact_cfg})
    _record_event(evt)
    return {"status": "ok", "event": target_name, "stage_type": stage_type, "execution_profile": stage_type, "stage_semantics": stage_semantics, "config": compact_cfg}


@app.get("/events")
def get_events(task_id: Optional[str] = None, limit: int = 200):
    try:
        return db.get_events(task_id, limit)
    except Exception:
        evts = state.history
        if task_id:
            evts = [e for e in evts if e.task_id == task_id]
        return [e.__dict__ for e in evts][-limit:]


@app.get("/tasks/{task_id}/stream")
async def stream_task_updates(task_id: str, request: Request):
    async def event_stream():
        snapshot = db.get_task_update_snapshot(task_id)
        yield _sse_frame(snapshot, event="hello")
        last_version = int(snapshot.get("version") or 0)
        while True:
            if await request.is_disconnected():
                break
            next_snapshot = await asyncio.to_thread(db.wait_for_task_update, task_id, last_version, 18.0)
            if next_snapshot is None:
                yield ": keepalive\n\n"
                continue
            last_version = int(next_snapshot.get("version") or 0)
            yield _sse_frame(next_snapshot, event="update")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/metrics")
def metrics():
    return metrics_plugin.snapshot()


@app.get("/models")
def list_models():
    return {
        "providers": list_all_provider_views(),
        "routing": {"default": get_default_provider_id(), "capability_overrides": {}},
        "registry": {
            "credentials": db.list_ai_credentials(),
            "models": db.list_ai_models(),
            "stage_bindings": db.get_stage_bindings(),
        },
    }


@app.post("/models/test", response_model=ProviderTestResponse)
def test_model(body: ModelTestRequest):
    payload = _payload_dict(body)
    provider_id = payload.get("provider_id")
    prompt = payload.get("prompt", "hello")
    if not provider_id:
        raise HTTPException(status_code=400, detail="provider_id required")
    try:
        cfg = get_registry_provider_config(provider_id)
        if cfg:
            out = model_registry.test_provider_config(cfg, prompt)
        else:
            out = model_registry.test_provider(provider_id, prompt)
        return {"output": out}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/ai-registry/overview")
def ai_registry_overview():
    return {
        "credentials": db.list_ai_credentials(),
        "models": db.list_ai_models(),
        "stage_bindings": db.get_stage_bindings(),
        "stages": EXECUTION_PROFILE_KEYS,
        "execution_profiles": EXECUTION_PROFILE_KEYS,
        "stage_semantics": sorted({"analysis", "planning", "design", "creation", "transformation", "verification", "delivery", "decision", "coordination"}),
        "provider_types": ["openai-compatible", "openai", "codex", "gemini"],
    }


@app.get("/ai-registry/credentials")
def get_ai_credentials():
    return {"credentials": db.list_ai_credentials()}


@app.post("/ai-registry/credentials")
def create_ai_credential(body: CreateAiCredentialRequest):
    payload = _payload_dict(body)
    name = str(payload.get("name") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    if not name or not base_url:
        raise HTTPException(status_code=400, detail="name and base_url required")
    try:
        return db.create_ai_credential(name=name, base_url=base_url, api_key_env=payload.get("api_key_env"), api_key=payload.get("api_key"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/ai-registry/credentials/{credential_id}")
def update_ai_credential(credential_id: str, body: UpdateAiCredentialRequest):
    row = db.update_ai_credential(credential_id, _payload_dict(body))
    if not row:
        raise HTTPException(status_code=404, detail="credential not found")
    return row


@app.delete("/ai-registry/credentials/{credential_id}", response_model=SimpleStatusResponse)
def delete_ai_credential(credential_id: str):
    try:
        ok = db.delete_ai_credential(credential_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="credential not found")
    return {"status": "ok"}


@app.post("/ai-registry/credentials/{credential_id}/discover-models")
def discover_ai_models(credential_id: str):
    cred = db.get_ai_credential_secret(credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="credential not found")
    api_key = str(cred.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="credential has no usable api key")
    return _discover_models_for_credential(cred.get("base_url") or "", api_key)


@app.post("/ai-registry/credentials/{credential_id}/test")
def test_ai_credential(credential_id: str):
    cred = db.get_ai_credential_secret(credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="credential not found")
    api_key = str(cred.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="credential has no usable api key")
    return test_credential_connection(cred.get("base_url") or "", api_key)


@app.post("/ai-registry/credentials/{credential_id}/models")
def quick_create_ai_model(credential_id: str, body: QuickCreateAiModelRequest):
    payload = _payload_dict(body)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="model name required")
    cred = db.get_ai_credential_secret(credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="credential not found")
    provider_type = str(payload.get("provider_type") or infer_provider_type(cred.get("base_url") or "", name)).strip()
    model_kind = str(payload.get("model_kind") or "llm").strip() or "llm"
    try:
        return db.create_ai_model(
            credential_id=credential_id,
            name=name,
            provider_type=provider_type,
            model_kind=model_kind,
            extra_config=payload.get("extra_config") if isinstance(payload.get("extra_config"), dict) else {},
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/ai-registry/models")
def get_ai_models(credential_id: Optional[str] = None):
    return {"models": db.list_ai_models(credential_id=credential_id)}


@app.post("/ai-registry/models")
def create_ai_model(body: CreateAiModelRequest):
    payload = _payload_dict(body)
    credential_id = str(payload.get("credential_id") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not credential_id or not name:
        raise HTTPException(status_code=400, detail="credential_id and name required")
    try:
        return db.create_ai_model(
            credential_id=credential_id,
            name=name,
            provider_type=str(payload.get("provider_type") or "openai-compatible"),
            model_kind=str(payload.get("model_kind") or "llm"),
            extra_config=payload.get("extra_config") if isinstance(payload.get("extra_config"), dict) else {},
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/ai-registry/models/{model_id}")
def update_ai_model(model_id: str, body: UpdateAiModelRequest):
    row = db.update_ai_model(model_id, _payload_dict(body))
    if not row:
        raise HTTPException(status_code=404, detail="model not found")
    return row


@app.delete("/ai-registry/models/{model_id}", response_model=SimpleStatusResponse)
def delete_ai_model(model_id: str):
    try:
        ok = db.delete_ai_model(model_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="model not found")
    return {"status": "ok"}


@app.post("/ai-registry/models/{model_id}/test", response_model=AiModelTestResponse)
def test_ai_model(model_id: str, body: Optional[AiModelTestRequest] = None):
    prompt = str(_payload_dict(body).get("prompt") or "ping").strip() or "ping"
    cfg = get_registry_provider_config(make_registry_provider_id(model_id))
    if not cfg:
        raise HTTPException(status_code=404, detail="model not found")
    try:
        return {"output": model_registry.test_provider_config(cfg, prompt), "provider": cfg.get("label") or cfg.get("model") or cfg.get("id")}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/ai-registry/stage-bindings", response_model=AiStageBindingsResponse)
def get_ai_stage_bindings():
    return {"bindings": db.get_stage_bindings(), "stages": EXECUTION_PROFILE_KEYS, "execution_profiles": EXECUTION_PROFILE_KEYS}


@app.put("/ai-registry/stage-bindings", response_model=AiStageBindingsResponse)
def update_ai_stage_bindings(body: UpdateAiStageBindingsRequest):
    payload = _payload_dict(body)
    bindings = payload.get("bindings") if isinstance(payload.get("bindings"), dict) else {}
    for stage in EXECUTION_PROFILE_KEYS:
        if stage in bindings:
            model_id = bindings.get(stage) or None
            if model_id and not db.get_ai_model(model_id):
                raise HTTPException(status_code=400, detail=f"model not found for stage {stage}")
            db.set_stage_binding(stage, model_id)
    return {"bindings": db.get_stage_bindings(), "stages": EXECUTION_PROFILE_KEYS, "execution_profiles": EXECUTION_PROFILE_KEYS}


@app.get("/capabilities")
def get_capabilities():
    _sync_capability_config_with_skills()
    return {**CAPA_CONFIG, "default_catalog": get_default_capability_catalog()}


@app.post("/capabilities")
def set_capabilities(body: SetCapabilitiesRequest):
    payload = _payload_dict(body)
    next_state = dict(CAPA_CONFIG)
    next_state.update({
        "vector_model": payload.get("vector_model", CAPA_CONFIG.get("vector_model", "")),
        "rerank_model": payload.get("rerank_model", CAPA_CONFIG.get("rerank_model", "")),
        "notes": payload.get("notes", CAPA_CONFIG.get("notes", "")),
        "deleted_catalog_ids": payload.get("deleted_catalog_ids", CAPA_CONFIG.get("deleted_catalog_ids", [])),
    })
    if isinstance(payload.get("catalog"), list):
        next_state["catalog"] = payload.get("catalog")
    if isinstance(payload.get("bindings"), list):
        next_state["bindings"] = payload.get("bindings")
    merged = merge_capability_settings(next_state)
    CAPA_CONFIG.clear()
    CAPA_CONFIG.update(merged)
    _sync_capability_config_with_skills()
    _persist_json(CAPA_CONFIG_PATH, CAPA_CONFIG)
    return {"status": "ok", **CAPA_CONFIG, "default_catalog": get_default_capability_catalog()}


@app.get("/mcp-servers")
def get_mcp_servers():
    return MCP_CONFIG


@app.post("/mcp-servers")
def set_mcp_servers(body: SetMcpServersRequest):
    merged = merge_mcp_settings(_payload_dict(body))
    MCP_CONFIG.clear()
    MCP_CONFIG.update(merged)
    os.makedirs(os.path.dirname(MCP_CONFIG_PATH), exist_ok=True)
    with open(MCP_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(MCP_CONFIG, f, ensure_ascii=False, indent=2)
    return {"status": "ok", **MCP_CONFIG}


@app.get("/skills")
def get_skills():
    return {**SKILL_CONFIG, "default_catalog": get_default_skill_catalog()}


@app.post("/skills")
def set_skills(body: SetSkillsRequest):
    payload = _payload_dict(body)
    next_state = dict(SKILL_CONFIG)
    next_state.update({
        "notes": payload.get("notes", SKILL_CONFIG.get("notes", "")),
    })
    if isinstance(payload.get("catalog"), list):
        next_state["catalog"] = payload.get("catalog")
    merged = merge_skill_settings(next_state)
    SKILL_CONFIG.clear()
    SKILL_CONFIG.update(merged)
    _persist_json(SKILL_CONFIG_PATH, SKILL_CONFIG)
    _sync_capability_config_with_skills(persist=True)
    return {"status": "ok", **SKILL_CONFIG, "default_catalog": get_default_skill_catalog()}

# ---- 静态前端 ----
static_dir = os.path.join(BASE_DIR, "frontend")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
