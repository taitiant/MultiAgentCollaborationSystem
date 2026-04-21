"""TestAgent: runs tests via CodeExecutionPlugin."""
from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, Callable, Optional

from core import Task, SystemState, new_message
from plugins.code_execution_plugin import CodeExecutionPlugin


class TestAgent:
    id = "tester"
    role_name = "TestAgent"
    domain = "software"
    capabilities: List[str] = []

    def __init__(self, executor=None, workspace_root: str = "workspace", stage_name: str = "testing", stage_type: str = "testing", progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.executor = executor or CodeExecutionPlugin()
        self.workspace_root = workspace_root
        self.stage_name = stage_name
        self.stage_type = stage_type
        self.progress_callback = progress_callback

    def _emit_progress(self, **payload: Any) -> None:
        if not self.progress_callback:
            return
        try:
            self.progress_callback(payload)
        except Exception:
            return

    def _exit_code(self, result: Dict[str, Any], default: int = 1) -> int:
        value = result.get("exit_code")
        try:
            return int(default if value is None else value)
        except Exception:
            return int(default)

    def _stage_config(self, task: Task) -> Dict[str, Any]:
        event_configs = (task.context or {}).get("event_configs") or {}
        cfg = dict(event_configs.get(self.stage_type, {})) if self.stage_type != self.stage_name else {}
        cfg.update(event_configs.get(self.stage_name, {}))
        return cfg

    def _build_exec_env(self, workspace: str) -> Dict[str, str]:
        env = dict(os.environ)
        code_root = os.path.join(workspace, 'code')
        extra_paths = [workspace]
        if os.path.isdir(code_root):
            extra_paths.insert(0, code_root)
        existing = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = os.pathsep.join(extra_paths + ([existing] if existing else []))
        return env

    def _discover_python_files(self, workspace: str) -> list[str]:
        py_files: list[str] = []
        for root, _, files in os.walk(workspace):
            for name in files:
                if name.endswith('.py'):
                    py_files.append(os.path.join(root, name))
        return sorted(py_files)

    def _discover_web_files(self, workspace: str) -> list[str]:
        web_files: list[str] = []
        for root, _, files in os.walk(workspace):
            for name in files:
                if name.endswith(('.html', '.css', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx')):
                    web_files.append(os.path.join(root, name))
        return sorted(web_files)

    def _discover_test_files(self, workspace: str) -> list[str]:
        hits: list[str] = []
        for root, _, files in os.walk(workspace):
            for name in files:
                if name.startswith('test_') and name.endswith('.py'):
                    hits.append(os.path.join(root, name))
        return sorted(hits)

    def _infer_project_stack(self, task: Task, py_files: list[str], web_files: list[str]) -> str:
        spec = str((task.context or {}).get('spec') or '').lower()
        if web_files and not py_files:
            return "web"
        if py_files and not web_files:
            return "python"
        web_hits = sum(
            1
            for token in ("浏览器", "web", "html", "css", "javascript", "js", "canvas", "前端", "flappy")
            if token in spec
        )
        python_hits = sum(
            1
            for token in ("python", "pygame", "命令行", "脚本")
            if token in spec
        )
        return "web" if web_hits >= python_hits else "python"

    def _format_source_file_list(self, workspace: str, files: list[str], empty_label: str) -> str:
        return "\n".join(
            f"- `{os.path.relpath(path, workspace)}`" for path in files[:20]
        ) or f"- {empty_label}"

    def _build_manual_checklist(self, task: Task, project_stack: str) -> str:
        spec = str((task.context or {}).get('spec') or '')
        lowered = spec.lower()
        if project_stack == "web":
            return (
                '1. 启动页面，确认游戏画面、标题与开始提示可正常显示。\n'
                '2. 验证点击或按键控制是否能够触发角色上升/开始游戏。\n'
                '3. 验证角色会持续受重力影响下落，且控制输入能明显改变运动轨迹。\n'
                '4. 验证障碍物会持续生成并向场景中移动，间隙可被穿越。\n'
                '5. 验证角色碰撞障碍物、顶部或地面后会正确进入失败状态。\n'
                '6. 验证成功通过障碍后分数会递增且显示刷新。\n'
                '7. 验证失败后可重新开始，并能重置状态与分数。\n'
                '8. 打开浏览器控制台，确认无明显脚本报错或资源加载失败。\n'
            )
        if "俄罗斯方块" in spec or "tetris" in lowered:
            return (
                '1. 启动游戏，确认窗口能正常打开且无异常退出。\n'
                '2. 验证方块自动下落。\n'
                '3. 验证左右移动。\n'
                '4. 验证旋转。\n'
                '5. 验证软降/硬降（如实现）。\n'
                '6. 验证方块落地锁定。\n'
                '7. 验证单行消除。\n'
                '8. 验证多行连续消除。\n'
                '9. 验证分数刷新。\n'
                '10. 验证堆叠到顶部时 Game Over。\n'
                '11. 验证重开/退出（如实现）。\n'
            )
        return (
            '1. 启动程序，确认主流程可进入且无异常退出。\n'
            '2. 验证需求对应的核心输入与交互链路可用。\n'
            '3. 验证核心状态变化、边界条件和失败分支。\n'
            '4. 验证关键结果输出或界面反馈正确。\n'
            '5. 验证异常输入或高风险操作不会导致程序崩溃。\n'
            '6. 验证重试、重置或退出流程（如实现）。\n'
        )

    def _manual_report(
        self,
        task: Task,
        workspace: str,
        *,
        project_stack: str,
        source_files: list[str],
        compile_result: Dict[str, Any],
        startup_result: Dict[str, Any] | None = None,
    ) -> str:
        spec = str((task.context or {}).get('spec') or '')
        file_list = self._format_source_file_list(
            workspace,
            source_files,
            '未发现可执行源码文件',
        )
        compile_exit = self._exit_code(compile_result, default=1)
        compile_command = str(compile_result.get("command") or "-")
        startup = dict(startup_result or {})
        startup_command = str(startup.get("command") or "-")
        startup_exit = startup.get("exit_code")
        startup_exit_text = "-" if startup_exit is None else str(self._exit_code(startup, default=1))
        if project_stack == "web":
            validation_label = "静态资源校验"
            validation_desc = "已执行 Web 静态文件存在性与引用完整性校验。" if compile_exit == 0 else "Web 静态文件校验失败，存在资源或入口问题。"
            risk_focus = (
                '- 浏览器渲染与交互体验仍需人工走查\n'
                '- 控制台报错、动画流畅度与碰撞体验需结合真实页面确认\n'
            )
        else:
            validation_label = "源码编译校验"
            validation_desc = "已执行 Python 编译校验，确认基础语法可通过。" if compile_exit == 0 else "Python 编译校验失败，当前代码尚未通过基础语法验证。"
            risk_focus = (
                '- 运行时交互与异常分支仍需人工走查\n'
                '- 性能、兼容性与边界行为需结合真实运行进一步确认\n'
            )
        startup_section = ""
        if startup:
            startup_desc = "入口导入/启动链校验通过。" if str(startup.get("error") or "") == "" and self._exit_code(startup, default=1) == 0 else "入口导入/启动链校验失败，当前不能保证 README 声明的运行方式可用。"
            startup_section = (
                '\n## 入口冒烟校验\n'
                f'- 执行命令：`{startup_command}`\n'
                f'- 退出码：{startup_exit_text}\n'
                f'- 说明：{startup_desc}\n'
            )
        return (
            '# 测试报告（自动回退）\n\n'
            '## 背景\n'
            '- 当前未发现可执行的 pytest 用例，已回退为“编译校验 + 手工测试清单”模式。\n'
            f'- 任务需求：{spec}\n\n'
            '## 自动校验\n'
            f'- 校验类型：{validation_label}\n'
            f'- 执行命令：`{compile_command}`\n'
            f'- 退出码：{compile_exit}\n'
            f'- 说明：{validation_desc}\n\n'
            f'{startup_section}\n'
            '## 手工测试清单\n'
            f'{self._build_manual_checklist(task, project_stack)}\n'
            '## 待人工重点确认\n'
            f'{risk_focus}\n'
            '## 本轮检测到的源码文件\n'
            f'{file_list}\n'
        )

    def _extract_local_web_references(self, text: str) -> list[str]:
        refs = re.findall(r'''(?:src|href)=["']([^"']+)["']''', text, flags=re.IGNORECASE)
        cleaned: list[str] = []
        for ref in refs:
            ref = str(ref or "").strip()
            if not ref or ref.startswith(("http://", "https://", "//", "data:", "#")):
                continue
            cleaned.append(ref.split("?", 1)[0].split("#", 1)[0].lstrip("./"))
        return cleaned

    def _run_web_static_check(self, workspace: str, web_files: list[str]) -> Dict[str, Any]:
        html_files = [path for path in web_files if path.endswith(".html")]
        script_files = [path for path in web_files if path.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"))]
        style_files = [path for path in web_files if path.endswith(".css")]
        missing_refs: list[str] = []
        empty_files: list[str] = []
        for path in web_files:
            rel_path = os.path.relpath(path, workspace)
            if not os.path.isfile(path):
                missing_refs.append(rel_path)
                continue
            try:
                content = open(path, "r", encoding="utf-8", errors="ignore").read()
            except Exception:
                content = ""
            if not content.strip():
                empty_files.append(rel_path)
            if path.endswith(".html"):
                for ref in self._extract_local_web_references(content):
                    ref_path = os.path.normpath(os.path.join(os.path.dirname(path), ref))
                    if not os.path.exists(ref_path):
                        missing_refs.append(os.path.relpath(ref_path, workspace))
        problems: list[str] = []
        if not html_files:
            problems.append("缺少 HTML 入口文件")
        if not script_files:
            problems.append("缺少脚本文件")
        if empty_files:
            problems.append("存在空文件：" + "、".join(sorted(set(empty_files))[:10]))
        if missing_refs:
            problems.append("存在缺失的本地资源引用：" + "、".join(sorted(set(missing_refs))[:10]))
        if problems:
            return {
                "command": "web-static-check",
                "stdout": "",
                "stderr": "；".join(problems),
                "exit_code": 1,
            }
        summary = [
            f"html={len(html_files)}",
            f"css={len(style_files)}",
            f"script={len(script_files)}",
            f"files={len(web_files)}",
        ]
        return {
            "command": "web-static-check",
            "stdout": " ".join(summary),
            "stderr": "",
            "exit_code": 0,
        }

    def _run_compile_check(self, workspace: str, py_files: list[str], web_files: list[str], project_stack: str) -> Dict[str, Any]:
        if project_stack == "web":
            return self._run_web_static_check(workspace, web_files)
        if py_files:
            quoted = ' '.join(f'"{path}"' for path in py_files[:300])
            compile_cmd = f'python -m py_compile {quoted}'
        else:
            compile_cmd = 'python -c "pass"'
        result = self.executor.run(compile_cmd, cwd=workspace, env=self._build_exec_env(workspace))
        return {"command": compile_cmd, **result}

    def _module_name_from_path(self, workspace: str, path: str) -> str:
        rel = os.path.relpath(path, workspace).replace("\\", "/")
        pure = PurePosixPath(rel)
        parts = list(pure.parts)
        if parts and parts[-1].endswith(".py"):
            parts[-1] = parts[-1][:-3]
        return ".".join(part for part in parts if part and part != "__init__")

    def _discover_startup_target(self, workspace: str, py_files: list[str]) -> str:
        preferred = [
            os.path.join(workspace, "code", "main.py"),
            os.path.join(workspace, "main.py"),
            os.path.join(workspace, "app", "main.py"),
            os.path.join(workspace, "src", "main.py"),
        ]
        normalized = {os.path.abspath(path): path for path in py_files}
        for candidate in preferred:
            if os.path.abspath(candidate) in normalized:
                return normalized[os.path.abspath(candidate)]
        return py_files[0] if py_files else ""

    def _run_startup_smoke_check(self, workspace: str, py_files: list[str]) -> Dict[str, Any]:
        target = self._discover_startup_target(workspace, py_files)
        if not target:
            return {"command": "-", "stdout": "", "stderr": "missing_startup_target", "exit_code": 1}
        module_name = self._module_name_from_path(workspace, target)
        if not module_name:
            return {"command": "-", "stdout": "", "stderr": "invalid_startup_module", "exit_code": 1}
        command = f'python -c "import importlib; importlib.import_module(\'{module_name}\')"'
        env = self._build_exec_env(workspace)
        env.setdefault("SDL_VIDEODRIVER", "dummy")
        env.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        result = self.executor.run(command, cwd=workspace, env=env)
        return {"command": command, "module": module_name, "target": os.path.relpath(target, workspace), **result}

    def _write_manual_report(
        self,
        task: Task,
        workspace: str,
        *,
        project_stack: str,
        source_files: list[str],
        compile_result: Dict[str, Any],
        startup_result: Dict[str, Any] | None = None,
    ) -> str:
        report_path = os.path.join(workspace, 'tests', 'manual_test_report.md')
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, 'w', encoding='utf-8') as handle:
            handle.write(
                self._manual_report(
                    task,
                    workspace,
                    project_stack=project_stack,
                    source_files=source_files,
                    compile_result=compile_result,
                    startup_result=startup_result,
                )
            )
        return report_path

    def act(self, task: Task, state: SystemState):
        workspace = task.workspace_path or os.path.join(self.workspace_root, task.task_id)
        testing_cfg = self._stage_config(task)
        py_files = self._discover_python_files(workspace)
        web_files = self._discover_web_files(workspace)
        source_files = sorted(set(py_files + web_files))
        project_stack = self._infer_project_stack(task, py_files, web_files)
        test_files = self._discover_test_files(workspace)
        artifacts = []

        if test_files:
            test_cmd = testing_cfg.get("full_test_command") or testing_cfg.get("test_command") or "pytest -q"
            self._emit_progress(
                progress_kind="test",
                progress_state="start",
                message=f"开始全面测试：{test_cmd}",
            )
            result = self.executor.run(test_cmd, cwd=workspace, env=self._build_exec_env(workspace))
            self._emit_progress(
                progress_kind="test",
                progress_state="done",
                message=f"全面测试完成（exit={self._exit_code(result, default=0)})",
            )
            artifacts.append({"type": "test_result", "uri": "inline", "content": {"command": test_cmd, **result}})

            no_tests = isinstance(result, dict) and int(result.get("exit_code", -1)) == 5
            if no_tests and py_files:
                self._emit_progress(
                    progress_kind="compile",
                    progress_state="start",
                    message="未发现可执行用例，改为编译校验",
                )
                compile_result = self._run_compile_check(workspace, py_files, web_files, project_stack)
                self._emit_progress(
                    progress_kind="compile",
                    progress_state="done",
                    message=f"编译校验完成（exit={self._exit_code(compile_result, default=0)})",
                )
                artifacts.append({"type": "compile_result", "uri": "inline", "content": compile_result})
                startup_result = None
                if self._exit_code(compile_result, default=1) == 0:
                    self._emit_progress(
                        progress_kind="startup",
                        progress_state="start",
                        message="正在执行入口冒烟校验",
                    )
                    startup_result = self._run_startup_smoke_check(workspace, py_files)
                    self._emit_progress(
                        progress_kind="startup",
                        progress_state="done",
                        message=f"入口冒烟校验完成（exit={self._exit_code(startup_result, default=0)})",
                    )
                    artifacts.append({"type": "startup_smoke_result", "uri": "inline", "content": startup_result})
                self._emit_progress(
                    progress_kind="report",
                    progress_state="start",
                    message="正在生成手工测试清单",
                )
                report_path = self._write_manual_report(
                    task,
                    workspace,
                    project_stack=project_stack,
                    source_files=source_files,
                    compile_result=compile_result,
                    startup_result=startup_result,
                )
                self._emit_progress(
                    progress_kind="report",
                    progress_state="done",
                    message="手工测试清单已生成",
                )
                artifacts.append({"type": "md", "uri": report_path, "mime": "text/markdown"})
        else:
            self._emit_progress(
                progress_kind="compile",
                progress_state="start",
                message="未发现自动化用例，执行编译校验",
            )
            compile_result = self._run_compile_check(workspace, py_files, web_files, project_stack)
            self._emit_progress(
                progress_kind="compile",
                progress_state="done",
                message=f"编译校验完成（exit={self._exit_code(compile_result, default=0)})",
            )
            artifacts.append({"type": "test_result", "uri": "inline", "content": compile_result})
            startup_result = None
            if project_stack == "python" and py_files and self._exit_code(compile_result, default=1) == 0:
                self._emit_progress(
                    progress_kind="startup",
                    progress_state="start",
                    message="正在执行入口冒烟校验",
                )
                startup_result = self._run_startup_smoke_check(workspace, py_files)
                self._emit_progress(
                    progress_kind="startup",
                    progress_state="done",
                    message=f"入口冒烟校验完成（exit={self._exit_code(startup_result, default=0)})",
                )
                artifacts.append({"type": "startup_smoke_result", "uri": "inline", "content": startup_result})
            self._emit_progress(
                progress_kind="report",
                progress_state="start",
                message="正在生成手工测试清单",
            )
            report_path = self._write_manual_report(
                task,
                workspace,
                project_stack=project_stack,
                source_files=source_files,
                compile_result=compile_result,
                startup_result=startup_result,
            )
            self._emit_progress(
                progress_kind="report",
                progress_state="done",
                message="手工测试清单已生成",
            )
            artifacts.append({"type": "md", "uri": report_path, "mime": "text/markdown"})

        return new_message(
            self.id,
            task,
            intent="run_tests",
            capabilities_used=[],
            artifacts=artifacts,
            metadata={},
        )


__all__ = ["TestAgent"]
