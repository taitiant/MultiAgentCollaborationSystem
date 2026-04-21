"""阶段评审服务，负责汇总证据、调用评审模型并生成统一评审结果。"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional

from orchestration.collab import CollaborationHub
from orchestration.planning.document_rules import _architecture_validation_issues, _docs_validation_issues
from orchestration.planning.stage_catalog import normalize_stage_type


def _model_failure_text(value: Any) -> str:
    text = str(value or "")
    if not text.startswith("["):
        return ""
    lowered = text.lower()
    if "error" in lowered or "empty response" in lowered or "disabled" in lowered:
        return text
    return ""


def _review_feedback_is_evidence_limited(feedback_text: str) -> bool:
    text = str(feedback_text or "")
    if not text:
        return False
    uncertain_markers = ("无法确认", "尚不能确认", "证据不足", "未见", "可能缺少", "大概率", "当前可见证据")
    return any(marker in text for marker in uncertain_markers)


def _extract_json_block(text: str) -> Dict[str, Any] | None:
    if not text:
        return None
    raw = text.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw)
    if not match:
        return None
    try:
        obj = json.loads(match.group(1))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


class WorkflowStageReviewer:
    """基于阶段产物、协作记录与验收标准生成评审结论。"""

    def __init__(
        self,
        *,
        base_dir: str,
        select_model: Callable[..., Any],
        emit_stage_progress: Callable[..., None],
        load_task_artifact_text: Callable[..., str],
        extract_agent_decision_candidates: Callable[[Dict[str, Any]], Dict[str, Any] | None],
        artifact_command_failed: Callable[[Dict[str, Any], set[str]], bool],
    ):
        self.base_dir = base_dir
        self.select_model = select_model
        self.emit_stage_progress = emit_stage_progress
        self.load_task_artifact_text = load_task_artifact_text
        self.extract_agent_decision_candidates = extract_agent_decision_candidates
        self.artifact_command_failed = artifact_command_failed

    def review(
        self,
        *,
        task,
        stage_name: str,
        payload: Dict[str, Any],
        stage_type: str | None = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        effective_stage_type = normalize_stage_type(stage_type or stage_name)
        event_configs = (task.context or {}).get("event_configs", {})
        stage_cfg = dict(event_configs.get(effective_stage_type, {})) if effective_stage_type != stage_name else {}
        stage_cfg.update(event_configs.get(stage_name, {}) if isinstance(event_configs, dict) else {})
        criteria = str(stage_cfg.get("acceptance_criteria") or "").strip()
        role = str(stage_cfg.get("planned_role") or "")
        summary = payload.get("output_summary") or {}
        artifacts = payload.get("artifacts") or []
        review_text_parts: List[str] = []
        total_chars = 0
        max_chars = 24000
        architecture_doc_text = ""
        docs_readme_text = ""
        workspace = os.path.abspath(task.workspace_path or os.path.join(self.base_dir, task.task_id))
        evidence_paths: List[str] = []
        seen_evidence = set()

        def add_evidence_path(path: str) -> None:
            abs_path = os.path.abspath(path)
            if abs_path in seen_evidence:
                return
            seen_evidence.add(abs_path)
            evidence_paths.append(abs_path)

        if effective_stage_type == "docs":
            add_evidence_path(os.path.join(workspace, "docs", "README.md"))
            add_evidence_path(os.path.join(workspace, "tests", "manual_test_report.md"))
            add_evidence_path(os.path.join(workspace, "analysis", "requirements.md"))
            add_evidence_path(os.path.join(workspace, "design", "architecture.md"))

        for art in artifacts[:12]:
            uri = str((art or {}).get("uri") or "")
            if not uri or uri == "inline":
                continue
            add_evidence_path(uri)

        for abs_uri in evidence_paths:
            if not abs_uri.startswith(workspace):
                continue
            low = abs_uri.lower()
            if not low.endswith((".md", ".txt", ".py", ".json", ".html", ".css", ".js", ".ts", ".tsx", ".jsx")):
                continue
            try:
                with open(abs_uri, "r", encoding="utf-8", errors="ignore") as handle:
                    text = handle.read(12000)
            except Exception:
                continue
            if not text:
                continue
            if effective_stage_type == "architecture" and abs_uri.lower().endswith("architecture.md"):
                architecture_doc_text = text
            if effective_stage_type == "docs" and abs_uri.lower().endswith(os.path.join("docs", "readme.md")):
                docs_readme_text = text
            left = max_chars - total_chars
            if left <= 0:
                break
            clipped = text[:left]
            review_text_parts.append(f"[{os.path.relpath(abs_uri, workspace)}]\n{clipped}")
            total_chars += len(clipped)

        review_text = "\n\n".join(review_text_parts)
        collaboration_text = CollaborationHub(task).build_stage_review_context(stage_name, local_limit=4, blackboard_limit=4, max_chars=3000)
        agent_decision_candidate = self.extract_agent_decision_candidates(payload)
        smoke_failed = self.artifact_command_failed(payload, {"smoke_test_result"})
        test_failed = self.artifact_command_failed(payload, {"test_result", "compile_result"})
        validation_signals: List[str] = []
        architecture_issues: List[str] = []
        docs_issues: List[str] = []
        if effective_stage_type == "coding":
            validation_signals.append(f"编码阶段冒烟结果：{'失败' if smoke_failed else '通过'}")
        if effective_stage_type == "testing":
            validation_signals.append(f"测试阶段执行结果：{'失败' if test_failed else '通过'}")
        if effective_stage_type == "architecture" and architecture_doc_text:
            requirements_text = self.load_task_artifact_text(task, os.path.join("analysis", "requirements.md"))
            architecture_issues = _architecture_validation_issues(
                str((task.context or {}).get("spec") or ""),
                requirements_text,
                architecture_doc_text,
            )
            if architecture_issues:
                validation_signals.append("架构文档结构校验未通过：" + "；".join(architecture_issues))
        if effective_stage_type == "docs" and docs_readme_text:
            docs_issues = _docs_validation_issues(docs_readme_text)
            if docs_issues:
                validation_signals.append("README 结构校验未通过：" + "；".join(docs_issues))
            else:
                validation_signals.append("README 结构校验通过：已检测到运行方式、文件结构、限制说明与测试结论。")
        if review_text_parts:
            validation_signals.append("注意：下方文件片段可能因长度限制被截断，不能仅凭片段结尾不完整就认定源文件本身被截断；应结合编译/测试结果综合判断。")

        if effective_stage_type == "testing":
            has_manual_report = any(str((art or {}).get("uri") or "").endswith("manual_test_report.md") for art in artifacts)
            source_files: List[str] = []
            for root_dir, _, file_names in os.walk(workspace):
                for file_name in file_names:
                    if file_name.endswith((".py", ".html", ".css", ".js", ".ts", ".tsx", ".jsx")):
                        source_files.append(os.path.join(root_dir, file_name))
            manual_report_missing_code = (
                "未发现 Python 源文件" in review_text
                or "未发现可执行源码文件" in review_text
            )
            if has_manual_report and source_files and not manual_report_missing_code and not self.artifact_command_failed(payload, {"test_result", "compile_result"}):
                has_web_source = any(path.endswith((".html", ".css", ".js", ".ts", ".tsx", ".jsx")) for path in source_files)
                feedback = "未发现可执行自动化用例，已按回退策略完成源码编译校验并生成手工测试清单，本轮测试阶段可暂时验收。"
                risks = [
                    "当前仍以编译校验和手工测试清单为主，后续迭代建议补充自动化测试。",
                    "UI 与交互体验仍需人工走查确认。",
                ]
                next_actions = [
                    "按 manual_test_report.md 执行关键玩法与 UI 手工验收。",
                    "后续补充至少一组核心逻辑自动化测试用例。",
                ]
                if has_web_source:
                    feedback = "未发现可执行自动化用例，已按回退策略完成 Web 静态校验并生成手工测试清单，本轮测试阶段可暂时验收。"
                    risks = [
                        "当前仍以静态校验和手工测试清单为主，真实浏览器交互仍需人工确认。",
                        "后续迭代建议补充浏览器侧自动化冒烟或交互测试。",
                    ]
                    next_actions = [
                        "按 manual_test_report.md 执行关键玩法、交互与控制台错误手工验收。",
                        "后续补充至少一组浏览器侧自动化测试或脚本化冒烟验证。",
                    ]
                return {
                    "review_status": "fallback",
                    "pass": True,
                    "score": 0.72,
                    "feedback": feedback,
                    "risks": risks,
                    "next_actions": next_actions,
                    "criteria": criteria,
                    "role": role,
                }

        if not criteria:
            return {
                "review_status": "skipped",
                "pass": None,
                "score": None,
                "feedback": "未配置验收标准，跳过自动评审。",
                "criteria": "",
                "role": role,
            }

        review_prompt = (
            "你是团队Leader，负责评审阶段产出是否满足验收标准。请仅输出 JSON。\n"
            f"阶段：{stage_name}\n"
            f"阶段类型：{effective_stage_type}\n"
            f"角色：{role or '-'}\n"
            f"验收标准：{criteria}\n"
            f"验证信号：{json.dumps(validation_signals, ensure_ascii=False)}\n"
            f"阶段执行方提出的人工决策候选：{json.dumps(agent_decision_candidate or {}, ensure_ascii=False)}\n"
            f"阶段输出摘要：{json.dumps(summary, ensure_ascii=False)}\n"
            f"产物：{json.dumps(artifacts[:8], ensure_ascii=False)}\n"
            f"阶段关键内容片段：\n{review_text[:24000]}\n"
            f"阶段协作记录：\n{collaboration_text[:5000]}\n"
            "如果需要人工决策，请把 human_decision_required 设为 true，并补充 decision_question、decision_options、decision_reason；"
            "否则 human_decision_required 设为 false。\n"
            "输出格式："
            "{\"pass\":true,\"score\":0.0,\"feedback\":\"...\",\"risks\":[\"...\"],\"next_actions\":[\"...\"],\"human_decision_required\":false,\"decision_question\":\"...\",\"decision_options\":[\"...\"],\"decision_reason\":\"...\"}"
        )

        raw = ""
        try:
            reviewer = self.select_model(task, "planning", [])
            self.emit_stage_progress(progress_callback, progress_kind="review", progress_state="start", message="正在进行阶段评审")
            raw = str(reviewer.generate(review_prompt, context=task.context))
            failure = _model_failure_text(raw)
            self.emit_stage_progress(progress_callback, progress_kind="review", progress_state="error" if failure else "done", message=f"阶段评审{'失败' if failure else '完成'}", error=failure or None)
            parsed = _extract_json_block(raw) or {}
        except Exception as exc:
            parsed = {}
            raw = f"[review error] {exc}"
            self.emit_stage_progress(progress_callback, progress_kind="review", progress_state="error", message="阶段评审失败", error=str(exc))

        if isinstance(parsed, dict) and isinstance(parsed.get("pass"), bool):
            if architecture_issues:
                merged_feedback = str(parsed.get("feedback") or "").strip()
                parsed["pass"] = False
                parsed["feedback"] = (((merged_feedback + "\n\n") if merged_feedback else "") + "架构文档存在确定性的结构问题：" + "；".join(architecture_issues))
                risks = parsed.get("risks") if isinstance(parsed.get("risks"), list) else []
                for issue in architecture_issues:
                    if issue not in risks:
                        risks.append(issue)
                parsed["risks"] = risks
            if effective_stage_type == "docs":
                merged_feedback = str(parsed.get("feedback") or "").strip()
                if docs_issues:
                    parsed["pass"] = False
                    parsed["feedback"] = (((merged_feedback + "\n\n") if merged_feedback else "") + "README 存在确定性的结构问题：" + "；".join(docs_issues))
                    risks = parsed.get("risks") if isinstance(parsed.get("risks"), list) else []
                    for issue in docs_issues:
                        if issue not in risks:
                            risks.append(issue)
                    parsed["risks"] = risks
                elif parsed.get("pass") is False and _review_feedback_is_evidence_limited(merged_feedback):
                    parsed["pass"] = True
                    parsed["feedback"] = (
                        "已基于 README 正文执行确定性结构校验，确认其覆盖运行方式、文件结构、限制说明与测试结论；"
                        "本轮将“证据不足型误判”自动纠正为通过。"
                        + ((f"\n\n原评审反馈：{merged_feedback}") if merged_feedback else "")
                    )
            return {
                "review_status": "ok",
                "pass": parsed.get("pass"),
                "score": parsed.get("score"),
                "feedback": parsed.get("feedback", ""),
                "risks": parsed.get("risks", []),
                "next_actions": parsed.get("next_actions", []),
                "human_decision_required": parsed.get("human_decision_required") is True,
                "decision_question": parsed.get("decision_question") or "",
                "decision_options": parsed.get("decision_options", []),
                "decision_reason": parsed.get("decision_reason") or "",
                "criteria": criteria,
                "role": role,
                "raw": raw[:1200],
            }

        artifact_count = int(summary.get("artifact_count") or 0)
        guessed_pass = artifact_count > 0 and not architecture_issues
        return {
            "review_status": "fallback",
            "pass": guessed_pass,
            "score": 0.55 if guessed_pass else 0.25,
            "feedback": ("评审模型返回非 JSON，使用启发式评估（按产物数量）。" if not architecture_issues else "评审模型返回非 JSON，且架构文档存在确定性的结构问题：" + "；".join(architecture_issues)),
            "criteria": criteria,
            "role": role,
            "raw": raw[:1200],
        }
