import io
import json
import zipfile

from core import SystemState, Task
from orchestration.capabilities.registry import merge_capability_settings
from orchestration.capabilities.runtime import CapabilityRuntime


def _demo_task(tmp_path):
    workspace = tmp_path / "task-runtime"
    workspace.mkdir(parents=True, exist_ok=True)
    return Task(
        task_id="task-runtime",
        domain="software",
        required_capabilities=[],
        context={"spec": "demo"},
        workspace_path=str(workspace),
    )


def test_doc_write_capability_generates_docx_artifact(tmp_path):
    runtime = CapabilityRuntime(
        capability_settings=merge_capability_settings(
            {
                "catalog": [
                    {
                        "id": "doc.write:v1",
                        "label": "文档写入",
                        "recommended_stage_types": ["docs"],
                        "runtime_defaults": {"output_formats": ["docx"]},
                    }
                ]
            }
        )
    )
    task = _demo_task(tmp_path)
    state = SystemState()
    written = []

    payload = runtime.apply_stage_capabilities(
        task=task,
        state=state,
        stage_name="docs_delivery",
        stage_type="docs",
        stage_label="文档写入",
        stage_config={"doc_output_formats": ["docx"]},
        capabilities=["doc.write:v1"],
        exec_result={"type": "md"},
        payload={
            "artifacts": [
                {
                    "type": "md",
                    "filename": "docs/README.md",
                    "content": "# 标题\n\n- 第一项\n- 第二项",
                }
            ],
            "metadata": {},
            "output_summary": {},
        },
        write_artifact=lambda artifact: written.append(dict(artifact)) or {**artifact, "uri": f"/tmp/{artifact['filename']}"},
    )

    docx_artifact = next(artifact for artifact in written if artifact["filename"] == "docs/README.docx")
    assert docx_artifact["mime"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with zipfile.ZipFile(io.BytesIO(docx_artifact["data"])) as archive:
        assert "word/document.xml" in archive.namelist()
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert "文档写入" in document_xml
    assert payload["metadata"]["doc_outputs"] == ["docx"]
    assert payload["output_summary"]["doc_output_formats"] == ["docx"]
    assert payload["capability_effects"][0]["status"] == "applied"


def test_doc_read_capability_extracts_text_from_docx_artifact(tmp_path):
    bootstrap = CapabilityRuntime()
    task = _demo_task(tmp_path)
    state = SystemState()
    docx_bytes = bootstrap.handlers["doc.write:v1"].apply(
        type(
            "DocContext",
            (),
            {
                "binding": lambda self=None: None,
                "capability_def": {"runtime_defaults": {}},
                "stage_config": {},
                "options": lambda self=None: {},
                "payload": {
                    "artifacts": [
                        {
                            "type": "md",
                            "filename": "docs/source.md",
                            "content": "# 标题\n\n第一段\n\n第二段",
                        }
                    ]
                },
                "stage_label": "文档写入",
                "stage_name": "docs_delivery",
                "capability_id": "doc.write:v1",
            },
        )()
    ).extra_artifacts[0]["data"]
    written = []

    payload = bootstrap.apply_stage_capabilities(
        task=task,
        state=state,
        stage_name="document_read",
        stage_type="docs",
        stage_label="文档读取",
        stage_config={},
        capabilities=["doc.read:v1"],
        exec_result={"type": "docx"},
        payload={
            "artifacts": [
                {
                    "type": "docx",
                    "filename": "documents/source.docx",
                    "data": docx_bytes,
                }
            ],
            "metadata": {},
            "output_summary": {},
        },
        write_artifact=lambda artifact: written.append(dict(artifact)) or {**artifact, "uri": f"/tmp/{artifact['filename']}"},
    )

    text_artifact = next(artifact for artifact in written if artifact["filename"] == "documents/source.txt")
    assert "标题" in text_artifact["content"]
    assert "第一段" in text_artifact["content"]
    assert payload["metadata"]["document_read"]["source_path"] == "documents/source.docx"
    assert payload["capability_effects"][0]["status"] == "applied"


def test_asset_generate_capability_emits_image_request_manifest(tmp_path):
    runtime = CapabilityRuntime(
        capability_settings=merge_capability_settings(
            {
                "bindings": [
                    {
                        "id": "asset-http",
                        "capability_id": "asset.generate:v1",
                        "binding_type": "http_api",
                        "transport": {
                            "url": "http://asset.service/generate",
                            "method": "POST",
                            "request_params": {"workflow": "image-generation"},
                        },
                        "response_contract": {"status": "success", "assets": []},
                        "defaults": {"asset_mode": "external_binding", "output_formats": ["png"]},
                    }
                ]
            }
        )
    )
    task = _demo_task(tmp_path)
    state = SystemState()
    written = []
    manifest = {
        "assets": [
            {"key": "mole", "prompt": "cartoon mole"},
            {"key": "hole", "prompt": "cartoon hole"},
        ]
    }

    payload = runtime.apply_stage_capabilities(
        task=task,
        state=state,
        stage_name="visual_assets",
        stage_type="assets",
        stage_label="视觉素材",
        stage_config={
            "capability_options": {
                "asset.generate:v1": {
                    "binding_id": "asset-http",
                }
            }
        },
        capabilities=["asset.generate:v1"],
        exec_result={"type": "md"},
        payload={
            "artifacts": [
                {
                    "type": "json",
                    "filename": "assets/manifest.json",
                    "content": json.dumps(manifest, ensure_ascii=False),
                }
            ],
            "metadata": {},
            "output_summary": {},
        },
        write_artifact=lambda artifact: written.append(dict(artifact)) or {**artifact, "uri": f"/tmp/{artifact['filename']}"},
    )

    request_artifact = next(
        artifact for artifact in written if artifact["filename"] == "assets/image_generation_request.json"
    )
    request_payload = json.loads(request_artifact["content"])
    assert request_payload["protocol_version"] == "macs.capability.v1"
    assert request_payload["asset_mode"] == "external_binding"
    assert request_payload["output_formats"] == ["png"]
    assert len(request_payload["assets"]) == 2
    assert request_payload["binding"]["binding_id"] == "asset-http"
    assert request_payload["binding"]["binding_type"] == "http_api"
    assert request_payload["invocation"]["request"]["input"]["action"] == "generate_assets"
    assert request_payload["invocation"]["adapter_manifest"]["request_params"]["workflow"] == "image-generation"
    assert request_payload["binding"]["transport"]["request_params"]["workflow"] == "image-generation"
    assert request_payload["binding"]["response_contract"]["status"] == "success"
    assert payload["metadata"]["asset_generation"]["mode"] == "external_binding"
    assert payload["capability_effects"][0]["binding_id"] == "asset-http"
    assert payload["capability_effects"][0]["status"] == "binding_ready"
    assert payload["capability_effects"][0]["invocation"]["contract"]["capability_id"] == "asset.generate:v1"


def test_unhandled_capability_is_recorded_as_planning_only(tmp_path):
    runtime = CapabilityRuntime(capability_settings=merge_capability_settings())
    task = _demo_task(tmp_path)

    payload = runtime.apply_stage_capabilities(
        task=task,
        state=SystemState(),
        stage_name="coding_loop",
        stage_type="coding",
        stage_label="核心开发",
        stage_config={},
        capabilities=["code.edit:v1"],
        exec_result={"type": "code"},
        payload={"artifacts": [], "metadata": {}, "output_summary": {}},
        write_artifact=lambda artifact: artifact,
    )

    assert payload["capability_effects"][0]["capability_id"] == "code.edit:v1"
    assert payload["capability_effects"][0]["status"] == "skipped"
    assert "未注册能力处理器" in payload["capability_effects"][0]["notes"][0]


def test_unhandled_capability_with_mcp_binding_generates_generic_request(tmp_path):
    runtime = CapabilityRuntime(
        capability_settings=merge_capability_settings(
            {
                "catalog": [
                    {
                        "id": "slides.render:v1",
                        "label": "PPT 渲染",
                        "supported_binding_types": ["mcp_server"],
                        "runtime_defaults": {"theme": "business"},
                    }
                ],
                "bindings": [
                    {
                        "id": "slides-mcp",
                        "capability_id": "slides.render:v1",
                        "binding_type": "mcp_server",
                        "mcp": {
                            "server_name": "pptx-mcp",
                            "transport": "stdio",
                            "tools": [{"name": "render_presentation", "args_template": {"topic": "{{topic}}"}}],
                        },
                    }
                ],
            }
        ),
        mcp_settings={
            "mcpServers": {
                "pptx-mcp": {
                    "command": "uvx",
                    "args": ["pptx-mcp"],
                }
            }
        },
    )
    task = _demo_task(tmp_path)
    written = []

    payload = runtime.apply_stage_capabilities(
        task=task,
        state=SystemState(),
        stage_name="slides_delivery",
        stage_type="delivery",
        stage_label="PPT 输出",
        stage_config={"capability_options": {"slides.render:v1": {"topic": "季度汇报", "binding_id": "slides-mcp"}}},
        capabilities=["slides.render:v1"],
        exec_result={"type": "md"},
        payload={"artifacts": [], "metadata": {}, "output_summary": {}},
        write_artifact=lambda artifact: written.append(dict(artifact)) or {**artifact, "uri": f"/tmp/{artifact['filename']}"},
    )

    assert payload["capability_effects"][0]["status"] == "binding_ready"
    request_artifact = next(artifact for artifact in written if artifact["type"] == "capability_request")
    request_payload = json.loads(request_artifact["content"])
    assert request_payload["binding"]["binding_type"] == "mcp_server"
    assert request_payload["binding"]["mcp"]["server_name"] == "pptx-mcp"
    assert request_payload["binding"]["mcp"]["launch"]["command"] == "uvx"
    assert request_payload["invocation"]["adapter_manifest"]["mcp"]["tools"][0]["name"] == "render_presentation"


def test_doc_write_can_prefer_external_binding_for_docx(tmp_path):
    runtime = CapabilityRuntime(
        capability_settings=merge_capability_settings(
            {
                "bindings": [
                    {
                        "id": "docx-mcp-write",
                        "capability_id": "doc.write:v1",
                        "binding_type": "mcp_server",
                        "mcp": {
                            "server_name": "docx-mcp",
                            "transport": "stdio",
                            "launch": {"command": "docx-mcp", "args": ["--transport", "stdio"]},
                            "tools": [{"name": "create_document", "args_template": {"file_path": "{{target_filename}}"}}],
                        },
                        "defaults": {"prefer_external_binding": True, "output_formats": ["docx"]},
                    }
                ]
            }
        )
    )
    task = _demo_task(tmp_path)
    written = []

    payload = runtime.apply_stage_capabilities(
        task=task,
        state=SystemState(),
        stage_name="docs_delivery",
        stage_type="docs",
        stage_label="文档输出",
        stage_config={"capability_options": {"doc.write:v1": {"binding_id": "docx-mcp-write"}}},
        capabilities=["doc.write:v1"],
        exec_result={"type": "md"},
        payload={
            "artifacts": [
                {"type": "md", "filename": "docs/README.md", "content": "# 标题\n\n正文内容"}
            ],
            "metadata": {},
            "output_summary": {},
        },
        write_artifact=lambda artifact: written.append(dict(artifact)) or {**artifact, "uri": f"/tmp/{artifact['filename']}"},
    )

    assert payload["capability_effects"][0]["status"] == "binding_ready"
    request_artifact = next(artifact for artifact in written if artifact["type"] == "capability_request")
    request_payload = json.loads(request_artifact["content"])
    assert request_payload["binding"]["binding_type"] == "mcp_server"
    assert request_payload["target_filename"].endswith(".docx")


def test_disabled_capability_is_skipped_by_runtime(tmp_path):
    runtime = CapabilityRuntime(
        capability_settings=merge_capability_settings(
            {
                "catalog": [
                    {
                        "id": "doc.write:v1",
                        "label": "文档写入",
                        "enabled": False,
                        "recommended_stage_types": ["docs"],
                    }
                ]
            }
        )
    )
    task = _demo_task(tmp_path)

    payload = runtime.apply_stage_capabilities(
        task=task,
        state=SystemState(),
        stage_name="docs_delivery",
        stage_type="docs",
        stage_label="交付文档",
        stage_config={},
        capabilities=["doc.write:v1"],
        exec_result={"type": "md"},
        payload={"artifacts": [], "metadata": {}, "output_summary": {}},
        write_artifact=lambda artifact: artifact,
    )

    assert payload["capability_effects"][0]["status"] == "disabled"
    assert "能力已禁用" in payload["capability_effects"][0]["notes"][0]


def test_capability_runtime_degrades_gracefully_when_handler_errors(tmp_path):
    class BrokenHandler:
        capability_id = "demo.capability:v1"

        def apply(self, _context):
            raise RuntimeError("boom")

    runtime = CapabilityRuntime(
        capability_settings=merge_capability_settings(
            {
                "catalog": [
                    {
                        "id": "demo.capability:v1",
                        "label": "演示能力",
                        "recommended_stage_types": ["docs"],
                    }
                ]
            }
        ),
        handlers={"demo.capability:v1": BrokenHandler()},
    )
    task = _demo_task(tmp_path)
    progress_events = []

    payload = runtime.apply_stage_capabilities(
        task=task,
        state=SystemState(),
        stage_name="docs_delivery",
        stage_type="docs",
        stage_label="交付文档",
        stage_config={},
        capabilities=["demo.capability:v1"],
        exec_result={"type": "md"},
        payload={"artifacts": [], "metadata": {}, "output_summary": {}},
        write_artifact=lambda artifact: artifact,
        progress_callback=lambda event: progress_events.append(dict(event)),
    )

    assert payload["capability_effects"][0]["status"] == "error"
    assert "boom" in payload["capability_effects"][0]["notes"][0]
    assert [event["progress_state"] for event in progress_events] == ["start", "error"]
