"""Simple model registry and router.

Config format (config/models.json):
{
  "providers": [
    {"id": "mock", "type": "mock"},
    {"id": "openai-main", "type": "openai", "model": "gpt-4o", "api_key_env": "OPENAI_API_KEY"}
  ],
  "routing": {
    "default": "mock",
    "capability_overrides": {
      "code.edit:v1": "openai-main"
    }
  }
}
"""
from __future__ import annotations
import json
import os
from typing import Dict, List, Optional, Any

from adapters.mock_adapter import MockAdapter
from adapters.openai_adapter import OpenAIAdapter
from adapters.gemini_adapter import GeminiAdapter
from adapters.codex_adapter import CodexAdapter
from core import BaseModelAdapter


ADAPTER_BUILDERS = {
    "mock": lambda cfg: MockAdapter(),
    "openai": lambda cfg: OpenAIAdapter(model_name=cfg.get("model", "gpt-4o"), config=cfg),
    "openai-compatible": lambda cfg: OpenAIAdapter(model_name=cfg.get("model", "gpt-4o"), config=cfg),
    "gemini": lambda cfg: GeminiAdapter(model_name=cfg.get("model", "gemini-flash-latest"), config=cfg),
    "codex": lambda cfg: CodexAdapter(model_name=cfg.get("model", "gpt-5.2"), config=cfg),
}


class ModelRegistry:
    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or ""
        self.providers: Dict[str, Dict[str, Any]] = {}
        self.routing: Dict[str, Any] = {"default": "", "capability_overrides": {}}
        self._instances: Dict[str, BaseModelAdapter] = {}
        if self.config_path:
            self._load()

    def _load(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, "r") as f:
                data = json.load(f)
                self.providers = {p["id"]: p for p in data.get("providers", [])}
                self.routing = data.get("routing", self.routing)
                self._normalize_routing()
        else:
            self.providers = {}
            self.routing = {"default": "", "capability_overrides": {}}

    def _normalize_routing(self):
        # default: keep backward compatibility when old config uses adapter type (e.g. "mock")
        default = self.routing.get("default")
        if default not in self.providers:
            compat = next((pid for pid, cfg in self.providers.items() if cfg.get("type") == default), None)
            if compat:
                self.routing["default"] = compat
            elif self.providers:
                self.routing["default"] = next(iter(self.providers.keys()))
            else:
                self.routing["default"] = ""
        overrides = self.routing.get("capability_overrides") or {}
        self.routing["capability_overrides"] = {k: v for k, v in overrides.items() if v in self.providers}

    def _persist(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        data = {
            "providers": list(self.providers.values()),
            "routing": self.routing,
        }
        with open(self.config_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # public API
    def list(self):
        return {
            "providers": list(self.providers.values()),
            "routing": self.routing,
        }

    def add_provider(self, provider_cfg: Dict[str, Any]):
        pid = provider_cfg.get("id")
        if not pid:
            raise ValueError("provider id required")
        self.providers[pid] = provider_cfg
        # reset instance cache
        self._instances.pop(pid, None)
        self._persist()

    def remove_provider(self, provider_id: str):
        if provider_id in self.providers:
            self.providers.pop(provider_id)
            self._instances.pop(provider_id, None)
            # cleanup routing references
            if self.routing.get("default") == provider_id:
                self.routing["default"] = next(iter(self.providers.keys()), "local-debug")
            overrides = self.routing.get("capability_overrides", {})
            self.routing["capability_overrides"] = {k: v for k, v in overrides.items() if v != provider_id}
            self._normalize_routing()
            self._persist()

    def update_routing(self, default: Optional[str] = None, capability_overrides: Optional[Dict[str, str]] = None):
        if default:
            if default not in self.providers:
                raise ValueError("default provider not found")
            self.routing["default"] = default
        if capability_overrides is not None:
            self.routing["capability_overrides"] = capability_overrides
        self._normalize_routing()
        self._persist()

    def resolve(self, capabilities: List[str]) -> BaseModelAdapter:
        provider_id = self.resolve_provider_id(capabilities)
        return self._get_instance(provider_id)

    def resolve_provider_id(self, capabilities: List[str] | None = None) -> str:
        caps = capabilities or []
        for cap in caps:
            pid = self.routing.get("capability_overrides", {}).get(cap)
            if pid and pid in self.providers:
                return pid
        if self.routing.get("default") in self.providers:
            return self.routing["default"]
        if self.providers:
            return next(iter(self.providers.keys()))
        return ""

    def get_by_id(self, provider_id: str | None, overrides: Optional[Dict[str, Any]] = None) -> BaseModelAdapter:
        resolved_id = provider_id if provider_id and provider_id in self.providers else self.resolve_provider_id()
        if overrides:
            cfg = dict(self.providers.get(resolved_id, {"id": resolved_id, "type": "mock"}))
            cfg.update({k: v for k, v in overrides.items() if v is not None})
            builder = ADAPTER_BUILDERS.get(cfg.get("type", "mock"), ADAPTER_BUILDERS["mock"])
            return builder(cfg)
        return self._get_instance(resolved_id)

    def build_adapter(self, provider_cfg: Dict[str, Any]) -> BaseModelAdapter:
        cfg = dict(provider_cfg or {})
        builder = ADAPTER_BUILDERS.get(cfg.get("type", "mock"), ADAPTER_BUILDERS["mock"])
        return builder(cfg)

    def test_provider(self, provider_id: str, prompt: str) -> str:
        adapter = self._get_instance(provider_id)
        return adapter.generate(prompt, context={"test": True})

    def test_provider_config(self, provider_cfg: Dict[str, Any], prompt: str) -> str:
        adapter = self.build_adapter(provider_cfg)
        return adapter.generate(prompt, context={"test": True})

    def _get_instance(self, provider_id: str) -> BaseModelAdapter:
        if provider_id in self._instances:
            return self._instances[provider_id]
        cfg = self.providers.get(provider_id)
        if not cfg:
            return MockAdapter()
        builder = ADAPTER_BUILDERS.get(cfg.get("type", "mock"), ADAPTER_BUILDERS["mock"])
        instance = builder(cfg)
        self._instances[provider_id] = instance
        return instance


__all__ = ["ModelRegistry"]
