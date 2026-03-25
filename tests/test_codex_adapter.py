from adapters.codex_adapter import CodexAdapter


def test_extract_text_from_multiline_sse_event():
    adapter = CodexAdapter(config={"api_key": "dummy"})
    sse = (
        'data: {"type":"response.output_text.delta",\n'
        'data: "delta":"你好"}\n'
        '\n'
        'data: {"type":"response.output_text.delta","delta":"，世界"}\n'
        '\n'
        'data: [DONE]\n'
    )

    text = adapter._extract_text_from_sse(sse)

    assert text == "你好，世界"


def test_extract_text_from_chat_choice_list_content():
    adapter = CodexAdapter(config={"api_key": "dummy"})
    sse = (
        'data: {"choices":[{"delta":{"content":[{"type":"output_text","text":"Flappy"}]}}]}\n'
        '\n'
        'data: {"choices":[{"delta":{"content":" Bird"}}]}\n'
    )

    text = adapter._extract_text_from_sse(sse)

    assert text == "Flappy Bird"


def test_generate_falls_back_to_chat_when_responses_errors(monkeypatch):
    adapter = CodexAdapter(config={"api_key": "dummy"})

    def fail_responses(prompt):
        raise RuntimeError("responses eof")

    def ok_chat(prompt):
        return "OK"

    monkeypatch.setattr(adapter, "_stream_responses_text", fail_responses)
    monkeypatch.setattr(adapter, "_stream_chat_text", ok_chat)

    assert adapter.generate("Reply with OK", context={}) == "OK"


def test_generate_uses_chat_when_responses_empty(monkeypatch):
    adapter = CodexAdapter(config={"api_key": "dummy"})

    monkeypatch.setattr(adapter, "_stream_responses_text", lambda prompt: "")
    monkeypatch.setattr(adapter, "_stream_chat_text", lambda prompt: "Fallback reply")

    assert adapter.generate("Reply", context={}) == "Fallback reply"
