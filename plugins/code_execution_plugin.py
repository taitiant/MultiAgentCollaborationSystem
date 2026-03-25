"""Code execution plugin with simple whitelist sandbox.
NOTE: For real security, replace with hardened sandbox.
"""
from __future__ import annotations
from typing import Dict, List
from core import BasePlugin, Event, SystemState
import subprocess
import shlex


class CodeExecutionPlugin:
    def __init__(self, allowed_cmds: List[str] | None = None, timeout_s: int = 120):
        self.allowed_cmds = set(allowed_cmds or ["pytest", "python", "python3", "pip", "ls", "cat"])
        self.timeout_s = timeout_s

    def run(self, command: str, cwd: str | None = None, env: Dict[str, str] | None = None) -> Dict[str, object]:
        argv = shlex.split(command)
        if not argv or argv[0] not in self.allowed_cmds:
            return {"error": "command_not_allowed", "cmd": argv[:1]}
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
            return {
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "exit_code": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"error": "timeout", "cmd": argv[:1]}

    def on_event(self, event: Event, state: SystemState) -> None:
        # no-op hook; execution is invoked directly by agents
        return None


__all__ = ["CodeExecutionPlugin"]
