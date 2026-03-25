"""OpenAI model adapter (OpenAI SDK v1.x).
- Reads API key from env (default OPENAI_API_KEY or provider config's api_key_env)
- Returns best-effort text even if SDK shape changes.
"""
from __future__ import annotations
import os
from typing import Dict, Any
from core import BaseModelAdapter

try:
    from openai import OpenAI
except ImportError:  # optional dependency
    OpenAI = None


class OpenAIAdapter:
    def __init__(self, model_name: str = "gpt-4o", config: Dict[str, Any] | None = None):
        self.model_name = model_name
        self.config = config or {}
        env_name = self.config.get("api_key_env") or "OPENAI_API_KEY"
        self.api_key = (os.getenv(env_name) if isinstance(env_name, str) and env_name else None) or self.config.get("api_key")
        self.base_url = (self.config.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        self.enabled = bool(self.api_key)
        self._client = None
        # 当 base_url 不是官方地址或 type=openai-compatible 时，走 HTTP 兼容模式，避免 SDK 传入 proxies 等参数问题
        self.http_only = self.config.get("type") == "openai-compatible" or "api.openai.com" not in self.base_url
        if self.enabled and OpenAI and not self.http_only:
            try:
                self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            except TypeError:
                self._client = None

    def generate(self, prompt: str, context: Dict[str, Any]) -> str:
        if not self.enabled:
            return "[openai disabled]" + prompt
        # HTTP 兼容模式
        if self.http_only or not self._client:
            try:
                import requests
                payload = {
                    "model": self.model_name,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": self.config.get("temperature", 0.2),
                }
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
                url = f"{self.base_url}/chat/completions"
                r = requests.post(url, json=payload, headers=headers, timeout=self.config.get("timeout", 30))
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                return f"[openai-http error] {e}"

        # SDK 模式
        try:
            resp = self._client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=self.config.get("temperature", 0.2),
            )
            content = resp.choices[0].message.content
            if isinstance(content, list):
                parts = []
                for part in content:
                    if hasattr(part, "text"):
                        parts.append(part.text)
                    elif isinstance(part, dict) and "text" in part:
                        parts.append(part["text"])
                return "\n".join(parts)
            return str(content)
        except Exception as e:  # runtime safety
            return f"[openai error] {e}"


__all__ = ["OpenAIAdapter"]
