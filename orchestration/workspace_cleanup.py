from __future__ import annotations

import os
from typing import Any, Dict, Optional

from core import Task

MANAGED_PROJECT_DIRS = {"src", "code", "app", "tests", "public", "assets", "scripts", "styles"}
PRESERVED_WORKSPACE_DIRS = {"analysis", "design", "docs", "plan", "logs", ".git", ".github", ".vscode", "__pycache__"}
MANAGED_ROOT_FILES = {
    "index.html",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig.json",
    "tsconfig.app.json",
    "vite.config.ts",
    "vite.config.js",
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "pytest.ini",
}


def cleanup_architecture_orphan_files(task: Task, workspace_root: str, stage_name: str, stage_def: Optional[Dict[str, Any]] = None) -> int:
    from orchestration.document_rules import (
        _extract_architecture_file_list,
        _infer_project_stack,
        _normalize_architecture_file_list,
    )
    from orchestration.stage_catalog import (
        normalize_stage_type,
    )

    stage_type = normalize_stage_type((stage_def or {}).get("stage_type") or stage_name)
    if stage_type != "coding":
        return 0

    workspace = os.path.abspath(task.workspace_path or workspace_root)
    arch_path = os.path.join(workspace, "design", "architecture.md")
    if not os.path.exists(arch_path):
        return 0

    try:
        with open(arch_path, "r", encoding="utf-8", errors="ignore") as f:
            arch_text = f.read()
    except Exception:
        return 0

    requirements_path = os.path.join(workspace, "analysis", "requirements.md")
    requirements_text = ""
    if os.path.exists(requirements_path):
        try:
            with open(requirements_path, "r", encoding="utf-8", errors="ignore") as f:
                requirements_text = f.read()
        except Exception:
            requirements_text = ""

    expected_files = [str(path).replace("\\", "/").lstrip("./") for path in _extract_architecture_file_list(arch_text)]
    if not expected_files:
        return 0

    project_stack = _infer_project_stack(str((task.context or {}).get("spec") or ""), requirements_text, arch_text, expected_files)
    expected_files = _normalize_architecture_file_list(expected_files, project_stack)
    cleanup_dirs = {path.split("/", 1)[0] for path in expected_files if "/" in path}
    cleanup_dirs &= MANAGED_PROJECT_DIRS
    if project_stack == "web":
        cleanup_dirs.update({"src", "public", "tests", "code"})
    else:
        cleanup_dirs.update({"code", "app", "tests", "src"})

    expected_set = set(expected_files)
    removed = 0

    for root, _, files in os.walk(workspace, topdown=False):
        rel_root = os.path.relpath(root, workspace).replace("\\", "/")
        if rel_root == ".":
            rel_root = ""
        root_name = rel_root.split("/", 1)[0] if rel_root else ""

        for file_name in files:
            rel_path = f"{rel_root}/{file_name}" if rel_root else file_name
            rel_path = rel_path.replace("\\", "/")
            should_manage = False
            if not rel_root:
                should_manage = rel_path in MANAGED_ROOT_FILES
            elif root_name in cleanup_dirs and root_name not in PRESERVED_WORKSPACE_DIRS:
                should_manage = True
            if not should_manage or rel_path in expected_set:
                continue
            abs_path = os.path.join(workspace, rel_path)
            try:
                if os.path.exists(abs_path):
                    os.remove(abs_path)
                    removed += 1
            except Exception:
                continue

        if rel_root and root_name in cleanup_dirs and root_name not in PRESERVED_WORKSPACE_DIRS:
            try:
                if os.path.isdir(root) and not os.listdir(root):
                    os.rmdir(root)
            except Exception:
                continue

    return removed
