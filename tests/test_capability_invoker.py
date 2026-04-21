from orchestration.capabilities.invoker import (
    build_capability_invoke_prompt,
    build_requested_capability_execution,
    extract_capability_invocations_from_text,
)


def test_extract_capability_invocations_from_text_strips_fence_and_returns_directives():
    text = (
        "# README\n\n"
        "交付说明正文。\n\n"
        "```capability.invoke\n"
        "{\"capability_id\":\"doc.write:v1\",\"input\":{\"target_filename\":\"documents/guide.docx\",\"content\":\"hello\"}}\n"
        "```\n"
    )

    cleaned, directives = extract_capability_invocations_from_text(text)

    assert "capability.invoke" not in cleaned
    assert cleaned.startswith("# README")
    assert directives == [
        {
            "capability_id": "doc.write:v1",
            "input": {
                "target_filename": "documents/guide.docx",
                "content": "hello",
            },
        }
    ]


def test_build_requested_capability_execution_merges_inputs_by_capability():
    capabilities, options = build_requested_capability_execution(
        [
            {"capability_id": "doc.write:v1", "input": {"target_filename": "documents/a.docx"}},
            {"capability_id": "doc.write:v1", "input": {"output_formats": ["docx"]}, "binding_id": "doc-http"},
        ]
    )

    assert capabilities == ["doc.write:v1"]
    assert options["doc.write:v1"]["target_filename"] == "documents/a.docx"
    assert options["doc.write:v1"]["output_formats"] == ["docx"]
    assert options["doc.write:v1"]["binding_id"] == "doc-http"


def test_build_capability_invoke_prompt_lists_contract_summary():
    prompt = build_capability_invoke_prompt(
        [
            {
                "id": "doc.write:v1",
                "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}},
                "output_schema": {"type": "object", "properties": {"artifacts": {"type": "array"}}},
                "invocation_hint": "写出 docx 文档",
                "supported_binding_types": ["http_api", "internal_tool"],
            }
        ]
    )

    assert "capability.invoke" in prompt
    assert "doc.write:v1" in prompt
    assert "content" in prompt
    assert "artifacts" in prompt
