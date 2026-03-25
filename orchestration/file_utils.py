from __future__ import annotations
import os
from typing import List

def ensure_workspace(root: str, task_id: str):
    for sub in ["analysis", "design", "code", "tests", "docs", "logs", "patches"]:
        os.makedirs(os.path.join(root, task_id, sub), exist_ok=True)


def write_text(root: str, task_id: str, rel_path: str, content: str) -> str:
    abs_path = os.path.join(root, task_id, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return abs_path
