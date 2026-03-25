"""Codex-compatible adapter using the Responses/SSE protocol."""
from __future__ import annotations

import os
import json
from typing import Any, Dict, List

import requests


class CodexAdapter:
    def __init__(self, model_name: str = "gpt-5.2", config: Dict[str, Any] | None = None):
        self.model_name = model_name
        self.config = config or {}
        env_name = self.config.get("api_key_env") or "CODEX_API_KEY"
        self.api_key = (os.getenv(env_name) if isinstance(env_name, str) and env_name else None) or self.config.get("api_key")
        self.base_url = (self.config.get("base_url") or "https://www.right.codes/codex/v1").rstrip("/")
        self.enabled = bool(self.api_key)

    def _endpoint(self) -> str:
        if self.base_url.endswith("/responses"):
            return self.base_url
        return f"{self.base_url}/responses"

    def _extract_text_from_json(self, data: Dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str) and data.get("output_text"):
            return data["output_text"]
        output = data.get("output") or []
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content") or []
            for part in content:
                if not isinstance(part, dict):
                    continue
                txt = part.get("text")
                if isinstance(txt, str) and txt:
                    texts.append(txt)
        return "\n".join(texts) if texts else str(data)

    def _iter_sse_payloads(self, text: str) -> List[str]:
        payloads: List[str] = []
        data_lines: List[str] = []
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            stripped = line.strip()
            if not stripped:
                if data_lines:
                    payloads.append("\n".join(data_lines))
                    data_lines = []
                continue
            if stripped.startswith(":"):
                continue
            if stripped.startswith("data:"):
                data_lines.append(stripped[5:].lstrip())
        if data_lines:
            payloads.append("\n".join(data_lines))
        return payloads

    def _extract_text_fragments(self, obj: Dict[str, Any]) -> List[str]:
        chunks: List[str] = []
        output_text = obj.get("output_text")
        if isinstance(output_text, str) and output_text:
            chunks.append(output_text)
        delta = obj.get("delta")
        if isinstance(delta, str) and delta:
            chunks.append(delta)
        output = obj.get("output") or []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content") or []
            for part in content:
                if not isinstance(part, dict):
                    continue
                txt = part.get("text")
                if isinstance(txt, str) and txt:
                    chunks.append(txt)
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            choice_delta = choice.get("delta") or {}
            content = choice_delta.get("content")
            if isinstance(content, str) and content:
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    txt = part.get("text")
                    if isinstance(txt, str) and txt:
                        chunks.append(txt)
        return chunks

    def _extract_text_from_sse(self, text: str) -> str:
        chunks: List[str] = []
        for payload in self._iter_sse_payloads(text):
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            chunks.extend(self._extract_text_fragments(obj))
        return "".join(chunks).strip()

    def _maybe_fix_mojibake(self, text: str) -> str:
        if not text:
            return text
        cjk_count = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        if cjk_count >= 4:
            return text
        markers = ("Ã", "æ", "ç", "ï¼", "â\x80")
        if not any(m in text for m in markers):
            return text
        try:
            repaired = text.encode("latin-1", errors="strict").decode("utf-8", errors="strict")
        except Exception:
            return text
        repaired_cjk_count = sum(1 for ch in repaired if "\u4e00" <= ch <= "\u9fff")
        return repaired if repaired_cjk_count > cjk_count else text

    def _stream_responses_text(self, prompt: str) -> str:
        payload = {
            "model": self.model_name,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt}
                    ],
                }
            ],
            "stream": True,
        }
        max_output_tokens = self.config.get("max_output_tokens")
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        with requests.post(
            self._endpoint(),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self.api_key}",
            },
            json=payload,
            timeout=self.config.get("timeout", 60),
            stream=True,
        ) as resp:
            resp.raise_for_status()
            lines = []
            for line in resp.iter_lines(decode_unicode=True):
                if line is None:
                    continue
                lines.append(line)
            return self._extract_text_from_sse("\n".join(lines))

    def _stream_chat_text(self, prompt: str) -> str:
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.config.get("temperature", 0.2),
            "stream": True,
        }
        max_tokens = self.config.get("max_tokens") or self.config.get("max_output_tokens")
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        url = self.base_url.rstrip("/") + "/chat/completions"
        with requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self.api_key}",
            },
            json=payload,
            timeout=self.config.get("timeout", 60),
            stream=True,
        ) as resp:
            resp.raise_for_status()
            chunks = []
            for line in resp.iter_lines(decode_unicode=True):
                if line is None:
                    continue
                chunks.append(line)
            sse_text = self._extract_text_from_sse("\n".join(chunks))
            if sse_text:
                return sse_text
            parts = []
            for raw in chunks:
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                payload_line = line[5:].strip()
                if not payload_line or payload_line == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload_line)
                except Exception:
                    continue
                parts.extend(self._extract_text_fragments(obj))
            return "".join(parts).strip()

    def _format_http_error(self, error: requests.HTTPError) -> str:
        resp = error.response
        if resp is not None:
            snippet = (resp.text or "")[:220].replace("\n", "\\n")
            return f"[codex http error] status={resp.status_code} body={snippet}"
        return f"[codex http error] {error}"

    def generate(self, prompt: str, context: Dict[str, Any]) -> str:
        if not self.enabled:
            return "[codex disabled]" + prompt
        errors: List[str] = []
        try:
            text = self._stream_responses_text(prompt)
            if text:
                return self._maybe_fix_mojibake(text)
        except requests.HTTPError as error:
            errors.append(self._format_http_error(error))
        except Exception as error:
            errors.append(f"[codex error] {error}")

        try:
            text = self._stream_chat_text(prompt)
            if text:
                return self._maybe_fix_mojibake(text)
        except requests.HTTPError as error:
            errors.append(self._format_http_error(error))
        except Exception as error:
            errors.append(f"[codex error] {error}")

        if errors:
            return errors[-1]
        return (
            f"[codex empty response] status=200 "
            f"model={self.model_name} endpoint={self._endpoint()}"
        )


__all__ = ["CodexAdapter"]
