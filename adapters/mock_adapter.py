"""Mock model adapter for local testing."""
from __future__ import annotations
from typing import Dict, Any
from core import BaseModelAdapter


class MockAdapter:
    def __init__(self, model_name: str = "mock", config: Dict[str, Any] | None = None):
        self.model_name = model_name
        self.config = config or {}

    def generate(self, prompt: str, context: Dict[str, Any]) -> str:
        return f"[mock:{self.model_name}] {prompt}\ncontext={context}"


__all__ = ["MockAdapter"]
