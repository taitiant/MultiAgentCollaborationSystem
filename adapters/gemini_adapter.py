"""Gemini native REST adapter using generateContent endpoint."""
from __future__ import annotations

import os
from typing import Any, Dict

import requests


class GeminiAdapter:
    def __init__(self, model_name: str = "gemini-flash-latest", config: Dict[str, Any] | None = None):
        self.model_name = model_name
        self.config = config or {}
        self.api_key = os.getenv(self.config.get("api_key_env", "GEMINI_API_KEY")) or self.config.get("api_key")
        self.base_url = (self.config.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.enabled = bool(self.api_key)

    def _endpoint(self) -> str:
        # Expected: https://.../v1beta/models/{model}:generateContent
        if self.base_url.endswith("/models"):
            return f"{self.base_url}/{self.model_name}:generateContent"
        return f"{self.base_url}/models/{self.model_name}:generateContent"

    def generate(self, prompt: str, context: Dict[str, Any]) -> str:
        if not self.enabled:
            return "[gemini disabled]" + prompt
        try:
            payload = {
                "contents": [
                    {"parts": [{"text": prompt}]}
                ]
            }
            resp = requests.post(
                self._endpoint(),
                headers={
                    "Content-Type": "application/json",
                    "X-goog-api-key": self.api_key,
                },
                json=payload,
                timeout=self.config.get("timeout", 30),
            )
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                return "[gemini empty]"
            parts = (candidates[0].get("content") or {}).get("parts") or []
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
            return "\n".join(texts) if texts else str(data)
        except Exception as e:
            return f"[gemini error] {e}"


__all__ = ["GeminiAdapter"]
