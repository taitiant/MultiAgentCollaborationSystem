"""模型选择服务，负责阶段配置解析、Provider 推断与注册中心模型装配。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import db
from core import Task


def infer_provider_type(base_url: str, model_name: str = "") -> str:
    """根据 base_url 与模型名推断 provider 类型。"""
    base = str(base_url or "").lower()
    model = str(model_name or "").lower()
    if "generativelanguage.googleapis.com" in base or "/v1beta" in base and "googleapis" in base:
        return "gemini"
    if "codex" in base or "codex" in model or model.startswith("gpt-5"):
        return "codex"
    if model.startswith("gemini"):
        return "gemini"
    return "openai-compatible"


def registry_provider_cfg(
    provider_id: str,
    model_row: Dict[str, Any],
    cred: Dict[str, Any],
    overrides: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """把注册中心里的模型与凭据记录转换成 ModelRegistry 可消费的 provider 配置。"""
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
        "label": f"{cred.get('name') or 'Registry'} / {model_row.get('name') or ''}",
    }
    extra = model_row.get("extra_config") or {}
    if isinstance(extra, dict):
        cfg.update({k: v for k, v in extra.items() if v not in (None, "")})
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None and v != ""})
    return cfg


class ModelSelector:
    """根据任务、阶段配置和注册中心状态选择实际使用的模型适配器。"""

    def __init__(self, model_registry: Any):
        self.model_registry = model_registry

    def select(self, task: Task, stage_name: str | None = None, capabilities: Optional[List[str]] = None):
        context = task.context or {}
        event_configs = context.get("event_configs") or {}
        stage_cfg = event_configs.get(stage_name, {}) if stage_name else {}
        explicit_provider = stage_cfg.get("model_provider") or context.get("default_model_provider")
        model_overrides = {
            "model": stage_cfg.get("model"),
            "temperature": stage_cfg.get("temperature"),
            "timeout": stage_cfg.get("timeout"),
            "base_url": stage_cfg.get("base_url"),
            "api_key_env": stage_cfg.get("api_key_env"),
            "api_key": stage_cfg.get("api_key"),
        }
        has_overrides = any(v is not None and v != "" for v in model_overrides.values())

        if explicit_provider:
            if str(explicit_provider).startswith("registry:model:"):
                model_id = str(explicit_provider).split(":", 2)[-1]
                model_row = db.get_ai_model(model_id)
                cred = db.get_ai_credential_secret((model_row or {}).get("credential_id", "")) if model_row else None
                if model_row and cred:
                    cfg = registry_provider_cfg(explicit_provider, model_row, cred, model_overrides if has_overrides else None)
                    return self.model_registry.build_adapter(cfg)
            return self.model_registry.get_by_id(explicit_provider, overrides=model_overrides if has_overrides else None)

        llm_models = [m for m in db.list_ai_models() if (m.get("model_kind") or "llm") == "llm"]
        if llm_models:
            model_id = llm_models[0].get("model_id", "")
            model_row = db.get_ai_model(model_id)
            cred = db.get_ai_credential_secret((model_row or {}).get("credential_id", "")) if model_row else None
            if model_row and cred:
                cfg = registry_provider_cfg(f"registry:model:{model_id}", model_row, cred, model_overrides if has_overrides else None)
                return self.model_registry.build_adapter(cfg)
        raise ValueError("未配置可用的 llm 模型，请先到 /models.html 注册并绑定模型")
