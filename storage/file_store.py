"""File-backed StorageAdapter implementation."""
from __future__ import annotations
import os
from typing import List
from core import StorageAdapter


class FileStore(StorageAdapter):
    def __init__(self, base_path: str = "workspace"):
        self.base_path = base_path
        os.makedirs(self.base_path, exist_ok=True)

    def _abs(self, task_id: str, rel_path: str) -> str:
        return os.path.join(self.base_path, task_id, rel_path)

    def put(self, task_id: str, rel_path: str, data: bytes) -> str:
        abs_path = self._abs(task_id, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "wb") as f:
            f.write(data)
        return abs_path

    def get(self, uri: str) -> bytes:
        with open(uri, "rb") as f:
            return f.read()

    def list(self, task_id: str, prefix: str = "") -> List[str]:
        task_root = os.path.join(self.base_path, task_id)
        results = []
        for root, _, files in os.walk(task_root):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), task_root)
                if rel.startswith(prefix):
                    results.append(os.path.join(task_root, rel))
        return results

    def delete(self, uri: str) -> None:
        if os.path.exists(uri):
            os.remove(uri)


__all__ = ["FileStore"]
