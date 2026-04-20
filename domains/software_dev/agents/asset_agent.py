"""AssetAgent: plans and generates placeholder visual assets."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

from core import Task, SystemState, new_message
from orchestration.collaboration import append_prompt_with_runtime_context


VISUAL_HINT_TOKENS = (
    "图片",
    "素材",
    "图标",
    "插画",
    "精灵",
    "sprite",
    "贴图",
    "角色",
    "背景",
    "按钮",
    "像素风",
    "美术",
    "地鼠",
    "地洞",
    "锤子",
    "icon",
    "asset",
)

HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
ALLOWED_KINDS = {
    "mole",
    "hole",
    "hammer",
    "bird",
    "pipe",
    "ground",
    "background",
    "button",
    "panel",
    "icon",
    "character",
    "badge",
    "generic",
}


def needs_visual_assets(spec: str, requirements_text: str = "", architecture_text: str = "") -> bool:
    merged = "\n".join([str(spec or ""), str(requirements_text or ""), str(architecture_text or "")]).lower()
    score = sum(1 for token in VISUAL_HINT_TOKENS if token.lower() in merged)
    if "svg" in merged or "image" in merged or "png" in merged:
        score += 1
    return score >= 2


def _extract_json_block(text: str) -> Dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _slug(value: str, fallback: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "_", str(value or "").strip().lower()).strip("_")
    return raw or fallback


def _safe_hex(value: str, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    if HEX_COLOR_RE.match(raw):
        return raw if raw.startswith("#") else f"#{raw}"
    return fallback


def _clamp_size(value: Any, fallback: int) -> int:
    try:
        number = int(value)
    except Exception:
        return fallback
    return max(96, min(640, number))


def _default_asset_specs(spec: str) -> List[Dict[str, Any]]:
    lowered = str(spec or "").lower()
    if "打地鼠" in spec or "mole" in lowered:
        return [
            {"key": "mole", "title": "地鼠", "kind": "mole", "width": 220, "height": 220, "primary_color": "#8d5a3b", "accent_color": "#f2c9a5", "label": "Mole", "purpose": "主角色", "prompt": "cartoon mole, front view, simple game sprite, clean background"},
            {"key": "hole", "title": "地洞", "kind": "hole", "width": 260, "height": 160, "primary_color": "#5c4033", "accent_color": "#2d2019", "label": "Hole", "purpose": "出生点", "prompt": "cartoon dirt hole, top view, simple game sprite"},
            {"key": "hammer", "title": "锤子", "kind": "hammer", "width": 260, "height": 220, "primary_color": "#e03131", "accent_color": "#8d6b4f", "label": "Hammer", "purpose": "交互道具", "prompt": "cartoon toy hammer, angled view, simple game prop"},
            {"key": "grass_bg", "title": "草地背景", "kind": "background", "width": 640, "height": 360, "primary_color": "#8ad66d", "accent_color": "#dff5cf", "label": "Grass", "purpose": "游戏背景", "prompt": "simple grass field background, bright casual game style"},
        ]
    if "flappy" in lowered or "bird" in lowered:
        return [
            {"key": "bird", "title": "小鸟", "kind": "bird", "width": 220, "height": 180, "primary_color": "#ffd43b", "accent_color": "#ff922b", "label": "Bird", "purpose": "主角色", "prompt": "cute flappy bird style bird, side view, simple sprite"},
            {"key": "pipe", "title": "管道", "kind": "pipe", "width": 180, "height": 420, "primary_color": "#37b24d", "accent_color": "#69db7c", "label": "Pipe", "purpose": "障碍物", "prompt": "green cartoon pipe obstacle, simple game asset"},
            {"key": "ground", "title": "地面", "kind": "ground", "width": 640, "height": 120, "primary_color": "#c0eb75", "accent_color": "#8f5b34", "label": "Ground", "purpose": "底部地面", "prompt": "simple side-scrolling grass ground strip, clean game asset"},
            {"key": "sky_bg", "title": "天空背景", "kind": "background", "width": 640, "height": 360, "primary_color": "#74c0fc", "accent_color": "#d0ebff", "label": "Sky", "purpose": "背景", "prompt": "bright sky background for casual mobile game"},
        ]
    return [
        {"key": "hero", "title": "主角色", "kind": "character", "width": 220, "height": 220, "primary_color": "#4c6ef5", "accent_color": "#dbe4ff", "label": "Hero", "purpose": "主视觉角色", "prompt": "friendly flat game character, clean outline, transparent background"},
        {"key": "button_primary", "title": "主要按钮", "kind": "button", "width": 280, "height": 120, "primary_color": "#2f7df6", "accent_color": "#74c0fc", "label": "Start", "purpose": "开始/确认按钮", "prompt": "rounded primary game button, modern casual style"},
        {"key": "background", "title": "背景", "kind": "background", "width": 640, "height": 360, "primary_color": "#e7f5ff", "accent_color": "#d3f9d8", "label": "BG", "purpose": "主背景", "prompt": "simple clean game background, casual style, no text"},
    ]


def _normalize_asset_spec(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    title = str(item.get("title") or item.get("name") or f"Asset {index}").strip() or f"Asset {index}"
    key = _slug(str(item.get("key") or title), f"asset_{index}")
    kind = str(item.get("kind") or "generic").strip().lower()
    if kind not in ALLOWED_KINDS:
        kind = "generic"
    width = _clamp_size(item.get("width"), 220 if kind != "background" else 640)
    height = _clamp_size(item.get("height"), 220 if kind != "background" else 360)
    return {
        "key": key,
        "title": title,
        "kind": kind,
        "width": width,
        "height": height,
        "primary_color": _safe_hex(item.get("primary_color"), "#4c6ef5"),
        "accent_color": _safe_hex(item.get("accent_color"), "#dbe4ff"),
        "label": str(item.get("label") or title).strip()[:24] or title,
        "purpose": str(item.get("purpose") or item.get("usage") or "视觉素材").strip() or "视觉素材",
        "prompt": str(item.get("prompt") or item.get("image_prompt") or item.get("raster_prompt") or f"{title} game asset").strip(),
        "file_name": f"assets/generated/{key}.svg",
        "prompt_file": f"assets/prompts/{key}.txt",
        "render_strategy": "svg_placeholder",
        "future_strategy": "image_model_ready",
    }


def _svg_document(width: int, height: int, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" fill="none">{body}</svg>'
    )


def _label_text(label: str, x: int, y: int, size: int = 20, fill: str = "#1f2933") -> str:
    return (
        f'<text x="{x}" y="{y}" text-anchor="middle" font-family="Arial, sans-serif" '
        f'font-size="{size}" fill="{fill}" font-weight="700">{xml_escape(label)}</text>'
    )


def _render_svg(asset: Dict[str, Any]) -> str:
    width = int(asset["width"])
    height = int(asset["height"])
    primary = asset["primary_color"]
    accent = asset["accent_color"]
    label = xml_escape(str(asset.get("label") or asset.get("title") or "Asset"))
    kind = str(asset.get("kind") or "generic")

    if kind == "hole":
        body = (
            f'<rect width="{width}" height="{height}" fill="#f8f9fa"/>'
            f'<ellipse cx="{width/2}" cy="{height*0.6}" rx="{width*0.36}" ry="{height*0.22}" fill="{primary}"/>'
            f'<ellipse cx="{width/2}" cy="{height*0.58}" rx="{width*0.26}" ry="{height*0.12}" fill="{accent}"/>'
            f'{_label_text(label, width // 2, int(height * 0.22), 22)}'
        )
        return _svg_document(width, height, body)
    if kind == "mole":
        body = (
            f'<rect width="{width}" height="{height}" rx="28" fill="#fff8e1"/>'
            f'<circle cx="{width*0.5}" cy="{height*0.56}" r="{min(width, height)*0.22}" fill="{primary}"/>'
            f'<circle cx="{width*0.42}" cy="{height*0.48}" r="{min(width, height)*0.04}" fill="#212529"/>'
            f'<circle cx="{width*0.58}" cy="{height*0.48}" r="{min(width, height)*0.04}" fill="#212529"/>'
            f'<ellipse cx="{width*0.5}" cy="{height*0.62}" rx="{width*0.08}" ry="{height*0.05}" fill="{accent}"/>'
            f'<circle cx="{width*0.34}" cy="{height*0.34}" r="{min(width, height)*0.06}" fill="{accent}"/>'
            f'<circle cx="{width*0.66}" cy="{height*0.34}" r="{min(width, height)*0.06}" fill="{accent}"/>'
            f'{_label_text(label, width // 2, int(height * 0.18), 20)}'
        )
        return _svg_document(width, height, body)
    if kind == "hammer":
        body = (
            f'<rect width="{width}" height="{height}" rx="28" fill="#fff4e6"/>'
            f'<rect x="{width*0.18}" y="{height*0.2}" width="{width*0.34}" height="{height*0.14}" rx="16" fill="{primary}"/>'
            f'<rect x="{width*0.48}" y="{height*0.25}" width="{width*0.12}" height="{height*0.5}" rx="18" fill="{accent}"/>'
            f'<circle cx="{width*0.54}" cy="{height*0.78}" r="{min(width, height)*0.06}" fill="#8d6b4f"/>'
            f'{_label_text(label, width // 2, int(height * 0.15), 20)}'
        )
        return _svg_document(width, height, body)
    if kind == "bird":
        body = (
            f'<rect width="{width}" height="{height}" rx="28" fill="#eef7ff"/>'
            f'<ellipse cx="{width*0.46}" cy="{height*0.54}" rx="{width*0.2}" ry="{height*0.18}" fill="{primary}"/>'
            f'<polygon points="{width*0.45},{height*0.56} {width*0.28},{height*0.5} {width*0.36},{height*0.68}" fill="{accent}"/>'
            f'<polygon points="{width*0.64},{height*0.54} {width*0.76},{height*0.5} {width*0.64},{height*0.62}" fill="#ff922b"/>'
            f'<circle cx="{width*0.4}" cy="{height*0.48}" r="{min(width, height)*0.035}" fill="#212529"/>'
            f'{_label_text(label, width // 2, int(height * 0.18), 20)}'
        )
        return _svg_document(width, height, body)
    if kind == "pipe":
        body = (
            f'<rect width="{width}" height="{height}" rx="20" fill="#f1fff1"/>'
            f'<rect x="{width*0.24}" y="{height*0.18}" width="{width*0.52}" height="{height*0.64}" rx="18" fill="{primary}"/>'
            f'<rect x="{width*0.16}" y="{height*0.12}" width="{width*0.68}" height="{height*0.16}" rx="18" fill="{accent}"/>'
            f'{_label_text(label, width // 2, int(height * 0.1), 18)}'
        )
        return _svg_document(width, height, body)
    if kind == "ground":
        body = (
            f'<rect width="{width}" height="{height}" fill="{accent}"/>'
            f'<rect y="{height*0.35}" width="{width}" height="{height*0.65}" fill="{primary}"/>'
            + "".join(
                f'<polygon points="{i},{height*0.35} {i+24},{height*0.1} {i+48},{height*0.35}" fill="#69db7c"/>'
                for i in range(0, width, 48)
            )
            + _label_text(label, width // 2, int(height * 0.84), 18, "#ffffff")
        )
        return _svg_document(width, height, body)
    if kind == "background":
        body = (
            "<defs>"
            f'<linearGradient id="bg" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="{accent}"/>'
            f'<stop offset="100%" stop-color="{primary}"/></linearGradient></defs>'
            f'<rect width="{width}" height="{height}" fill="url(#bg)"/>'
            f'<circle cx="{width*0.18}" cy="{height*0.2}" r="{min(width, height)*0.08}" fill="#fff3bf" opacity="0.9"/>'
            f'<ellipse cx="{width*0.72}" cy="{height*0.26}" rx="{width*0.12}" ry="{height*0.08}" fill="#ffffff" opacity="0.8"/>'
            f'<ellipse cx="{width*0.82}" cy="{height*0.28}" rx="{width*0.1}" ry="{height*0.07}" fill="#ffffff" opacity="0.7"/>'
            f'{_label_text(label, width // 2, int(height * 0.88), 22, "#ffffff")}'
        )
        return _svg_document(width, height, body)
    if kind == "button":
        body = (
            f'<rect x="8" y="8" width="{width-16}" height="{height-16}" rx="28" fill="{primary}"/>'
            f'<rect x="16" y="16" width="{width-32}" height="{height*0.32}" rx="20" fill="{accent}" opacity="0.3"/>'
            f'{_label_text(label, width // 2, int(height * 0.58), 28, "#ffffff")}'
        )
        return _svg_document(width, height, body)
    if kind == "panel":
        body = (
            f'<rect width="{width}" height="{height}" rx="24" fill="{primary}" opacity="0.12"/>'
            f'<rect x="10" y="10" width="{width-20}" height="{height-20}" rx="20" fill="#ffffff" stroke="{primary}" stroke-width="6"/>'
            f'{_label_text(label, width // 2, int(height * 0.54), 24)}'
        )
        return _svg_document(width, height, body)
    if kind == "icon":
        body = (
            f'<rect width="{width}" height="{height}" rx="24" fill="{accent}"/>'
            f'<circle cx="{width*0.5}" cy="{height*0.46}" r="{min(width, height)*0.2}" fill="{primary}"/>'
            f'{_label_text(label, width // 2, int(height * 0.84), 18)}'
        )
        return _svg_document(width, height, body)
    if kind == "character":
        body = (
            f'<rect width="{width}" height="{height}" rx="28" fill="#f8f9fa"/>'
            f'<circle cx="{width*0.5}" cy="{height*0.34}" r="{min(width, height)*0.14}" fill="{accent}"/>'
            f'<rect x="{width*0.34}" y="{height*0.48}" width="{width*0.32}" height="{height*0.24}" rx="24" fill="{primary}"/>'
            f'<rect x="{width*0.28}" y="{height*0.72}" width="{width*0.12}" height="{height*0.16}" rx="12" fill="{primary}"/>'
            f'<rect x="{width*0.6}" y="{height*0.72}" width="{width*0.12}" height="{height*0.16}" rx="12" fill="{primary}"/>'
            f'{_label_text(label, width // 2, int(height * 0.16), 20)}'
        )
        return _svg_document(width, height, body)
    if kind == "badge":
        body = (
            f'<rect width="{width}" height="{height}" rx="24" fill="{primary}"/>'
            f'<circle cx="{width*0.5}" cy="{height*0.46}" r="{min(width, height)*0.22}" fill="{accent}"/>'
            f'{_label_text(label, width // 2, int(height * 0.84), 18, "#ffffff")}'
        )
        return _svg_document(width, height, body)
    body = (
        f'<rect width="{width}" height="{height}" rx="24" fill="{accent}"/>'
        f'<rect x="16" y="16" width="{width-32}" height="{height-32}" rx="20" fill="{primary}" opacity="0.18"/>'
        f'{_label_text(label, width // 2, height // 2, 24)}'
    )
    return _svg_document(width, height, body)


class AssetAgent:
    id = "asset-designer"
    role_name = "AssetAgent"
    domain = "software"
    capabilities: List[str] = ["asset.generate:v1"]

    def __init__(
        self,
        model_adapter=None,
        stage_name: str = "assets",
        stage_type: str = "assets",
        prompt_template: str | None = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.model_adapter = model_adapter
        self.stage_name = stage_name
        self.stage_type = stage_type
        self.prompt_template = prompt_template
        self.progress_callback = progress_callback

    def _emit_progress(self, **payload: Any) -> None:
        if not self.progress_callback:
            return
        try:
            self.progress_callback(payload)
        except Exception:
            return

    def _load_text(self, task: Task, rel_path: str) -> str:
        workspace = os.path.abspath(task.workspace_path or "")
        if not workspace:
            return ""
        abs_path = os.path.join(workspace, rel_path)
        if not os.path.exists(abs_path):
            return ""
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as handle:
                return handle.read()
        except Exception:
            return ""

    def _generate_manifest(self, task: Task, prompt: str) -> Dict[str, Any]:
        self._emit_progress(progress_kind="model", progress_state="start", message="正在规划视觉素材")
        try:
            raw = str(self.model_adapter.generate(prompt, context=task.context))
        except Exception as exc:
            self._emit_progress(progress_kind="model", progress_state="error", message="视觉素材规划失败", error=str(exc))
            raise
        parsed = _extract_json_block(raw) or {}
        self._emit_progress(progress_kind="model", progress_state="done", message="视觉素材规划完成")
        return {"raw": raw, "parsed": parsed}

    def _build_prompt(self, task: Task) -> str:
        spec = str((task.context or {}).get("spec") or "")
        requirements_text = self._load_text(task, os.path.join("analysis", "requirements.md"))
        architecture_text = self._load_text(task, os.path.join("design", "architecture.md"))
        base_prompt = self.prompt_template or (
            "你是视觉素材设计师。请根据需求与架构，规划当前任务所需的视觉素材，并尽量为后续图像模型生成准备可复用提示词。\n"
            "只输出严格 JSON，不要解释。\n"
            "输出格式："
            "{\"style\":\"...\",\"assets\":[{\"key\":\"mole\",\"title\":\"地鼠\",\"kind\":\"mole|hole|hammer|bird|pipe|ground|background|button|panel|icon|character|badge|generic\",\"purpose\":\"...\",\"width\":220,\"height\":220,\"primary_color\":\"#8d5a3b\",\"accent_color\":\"#f2c9a5\",\"label\":\"Mole\",\"prompt\":\"...\"}]}"
        )
        prompt = (
            f"{base_prompt}\n\n"
            f"任务需求：{spec}\n"
            f"需求文档摘要：{requirements_text[:4000]}\n"
            f"架构文档摘要：{architecture_text[:4000]}\n"
            "约束：\n"
            "- 优先输出 3~6 个最关键素材，不要泛滥生成无关资源；\n"
            "- 目前渲染层默认先落 SVG 占位素材，因此尺寸、颜色、用途必须明确；\n"
            "- prompt 字段要可直接给未来的生图模型使用；\n"
            "- file_name 不用输出，系统会自动生成。\n"
        )
        return append_prompt_with_runtime_context(prompt, task, self.stage_name)

    def act(self, task: Task, state: SystemState):
        spec = str((task.context or {}).get("spec") or "")
        prompt = self._build_prompt(task)
        model_output = self._generate_manifest(task, prompt)
        parsed = model_output.get("parsed") if isinstance(model_output.get("parsed"), dict) else {}
        raw_assets = parsed.get("assets") if isinstance(parsed.get("assets"), list) else []
        if not raw_assets:
            raw_assets = _default_asset_specs(spec)
        assets = [_normalize_asset_spec(item, index + 1) for index, item in enumerate(raw_assets[:6]) if isinstance(item, dict)]
        if not assets:
            assets = [_normalize_asset_spec(item, index + 1) for index, item in enumerate(_default_asset_specs(spec)[:4])]

        manifest = {
            "version": 1,
            "style": str(parsed.get("style") or "placeholder-svg"),
            "render_mode": "svg_placeholder",
            "future_image_generation_ready": True,
            "assets": assets,
        }

        markdown_lines = [
            "# 视觉素材清单",
            "",
            f"- 素材数量：{len(assets)}",
            "- 当前渲染策略：SVG 占位图",
            "- 后续扩展：可直接把对应 prompt 接给图像模型生成正式 PNG/WebP 资源",
            "",
            "| 标识 | 标题 | 类型 | 用途 | 尺寸 | 颜色 | 未来生图 Prompt |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for asset in assets:
            markdown_lines.append(
                f"| `{asset['key']}` | {asset['title']} | `{asset['kind']}` | {asset['purpose']} | "
                f"{asset['width']}x{asset['height']} | `{asset['primary_color']}` / `{asset['accent_color']}` | {asset['prompt']} |"
            )

        artifacts: List[Dict[str, Any]] = [
            {
                "type": "md",
                "filename": "design/assets.md",
                "content": "\n".join(markdown_lines) + "\n",
                "mime": "text/markdown",
            },
            {
                "type": "json",
                "filename": "assets/manifest.json",
                "content": json.dumps(manifest, ensure_ascii=False, indent=2),
                "mime": "application/json",
            },
        ]
        for asset in assets:
            artifacts.append(
                {
                    "type": "asset_prompt",
                    "filename": asset["prompt_file"],
                    "content": asset["prompt"] + "\n",
                    "mime": "text/plain",
                    "label": asset["title"],
                }
            )
            artifacts.append(
                {
                    "type": "asset",
                    "filename": asset["file_name"],
                    "content": _render_svg(asset),
                    "mime": "image/svg+xml",
                    "label": asset["title"],
                }
            )

        return new_message(
            self.id,
            task,
            intent="generate_assets",
            capabilities_used=self.capabilities,
            artifacts=artifacts,
            metadata={
                "style": manifest["style"],
                "render_mode": manifest["render_mode"],
                "future_image_generation_ready": True,
                "asset_count": len(assets),
                "raw_model_output": str(model_output.get("raw") or "")[:2000],
            },
        )
