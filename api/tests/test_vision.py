"""Vision pipeline tests: tolerant parser (F#24), schema enforcement (F#27),
OpenAI-protocol backend (Dev2_007).
"""
from __future__ import annotations

import json
import logging

import pytest

from app.services import vision as vision_mod
from app.services.vision import (
    _extract_suggestions,
    _parse_suggestions,
    describe_photo,
)


def test_parse_suggestions_object_shape():
    """Happy path: model returned the expected wrapped object."""
    raw = '{"suggestions": [{"name": "M3 Bolt", "category": "fastener", "confidence": 0.9}]}'
    out = _parse_suggestions(raw)
    assert out == [{"name": "M3 Bolt", "category": "fastener", "confidence": 0.9}]


def test_parse_suggestions_bare_list_shape():
    """Bug under fix: model returned a bare JSON array — must still parse."""
    raw = '[{"name": "Car Key Fob", "category": "electronics"}]'
    out = _parse_suggestions(raw)
    assert out == [{"name": "Car Key Fob", "category": "electronics"}]


def test_parse_suggestions_markdown_wrapped_object():
    """Markdown fences must still be stripped for the object shape."""
    raw = '```json\n{"suggestions": [{"name": "Wallet", "category": "other"}]}\n```'
    out = _parse_suggestions(raw)
    assert out == [{"name": "Wallet", "category": "other"}]


def test_parse_suggestions_markdown_wrapped_bare_list():
    """Markdown fences + bare list (the exact shape qwen3-vl:2b emitted on photo_id=13)."""
    raw = '```json\n[{"name": "Y Logo Key Fob", "category": "electronics"}]\n```'
    out = _parse_suggestions(raw)
    assert out == [{"name": "Y Logo Key Fob", "category": "electronics"}]


def test_parse_suggestions_malformed_returns_empty_and_warns(caplog):
    """Non-JSON content must return [] AND emit a WARN log so schema drift
    is debuggable instead of silently swallowed."""
    raw = "I cannot identify this image."
    with caplog.at_level(logging.WARNING, logger="app.services.vision"):
        out = _parse_suggestions(raw, photo_id=42)
    assert out == []
    warn_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warn_records, "expected at least one WARN log on parse failure"


def test_parse_suggestions_wrong_key_returns_empty_and_warns(caplog):
    """JSON object missing the 'suggestions' key must return [] AND warn."""
    raw = '{"items": [{"name": "X"}]}'
    with caplog.at_level(logging.WARNING, logger="app.services.vision"):
        out = _parse_suggestions(raw, photo_id=99)
    assert out == []
    warn_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warn_records, "expected at least one WARN log on missing 'suggestions' key"


def test_parse_suggestions_drops_items_without_name():
    """Items in the list missing a 'name' field must be dropped."""
    raw = '{"suggestions": [{"name": "Keep"}, {"category": "drop"}, {"name": ""}]}'
    out = _parse_suggestions(raw)
    assert out == [{"name": "Keep"}]


# ---------------------------------------------------------------------------
# Direct tests for the _extract_suggestions helper (post-Step-3 refactor).
# ---------------------------------------------------------------------------


def test_extract_suggestions_dict_shape():
    out = _extract_suggestions({"suggestions": [{"name": "X"}]}, None, "")
    assert out == [{"name": "X"}]


def test_extract_suggestions_list_shape():
    out = _extract_suggestions([{"name": "Y"}], None, "")
    assert out == [{"name": "Y"}]


def test_extract_suggestions_unexpected_scalar_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.vision"):
        out = _extract_suggestions(42, photo_id=7, sample="42")
    assert out == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_extract_suggestions_dict_with_non_list_value_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.vision"):
        out = _extract_suggestions({"suggestions": "not a list"}, photo_id=8, sample="")
    assert out == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Dev2_007: OpenAI-protocol interchangeable vision backend.
# Tests mock the openai.OpenAI client (not urllib.request) and assert that
# describe_photo uses the OpenAI chat completions interface with image_url
# format, making the backend swappable between Ollama and hosted providers.
# ---------------------------------------------------------------------------


def _write_jpeg(tmp_path, valid_jpeg_bytes):
    """Helper: drop a valid 1x1 JPEG onto disk and return the path."""
    p = tmp_path / "warm.jpg"
    p.write_bytes(valid_jpeg_bytes)
    return str(p)


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeChatCompletion:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


def _patch_openai(monkeypatch, captured: dict, response_content: str):
    """Replace the openai.OpenAI constructor so describe_photo's client
    captures its call kwargs and returns a canned response."""

    class _FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeChatCompletion(response_content)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

    monkeypatch.setattr(vision_mod, "openai", type("openai", (), {"OpenAI": _FakeClient}))


def test_suggest_response_schema_shape():
    """The Pydantic schema must have a 'suggestions' array of name-bearing items."""
    assert hasattr(vision_mod, "SuggestResponseSchema"), (
        "vision module must expose SuggestResponseSchema (Finding #27)"
    )
    schema = vision_mod.SuggestResponseSchema.model_json_schema()
    assert schema.get("type") == "object"
    props = schema.get("properties", {})
    assert "suggestions" in props, schema
    assert props["suggestions"]["type"] == "array"
    item_schema = props["suggestions"]["items"]
    if "$ref" in item_schema:
        ref = item_schema["$ref"].rsplit("/", 1)[-1]
        item_schema = schema["$defs"][ref]
    assert "name" in item_schema.get("properties", {}), item_schema
    assert "name" in item_schema.get("required", []), item_schema


def test_describe_photo_uses_openai_client(tmp_path, valid_jpeg_bytes, monkeypatch):
    """describe_photo must use the openai.OpenAI client with base_url + api_key,
    send image via data-URI image_url format, and use response_format json_object."""
    captured: dict = {}
    _patch_openai(
        monkeypatch, captured,
        response_content='{"suggestions": [{"name": "M3 Bolt"}]}',
    )

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    suggestions, _elapsed = describe_photo(
        photo_path, "http://fake:11434/v1", "test-key", "qwen3-vl:2b",
    )

    assert suggestions == [{"name": "M3 Bolt"}]

    # Verify OpenAI client was constructed with our base_url + api_key.
    ck = captured["client_kwargs"]
    assert ck["base_url"] == "http://fake:11434/v1"
    assert ck["api_key"] == "test-key"

    # Verify the chat completion call used OpenAI image_url format.
    messages = captured["messages"]
    user_msg = messages[0]
    assert user_msg["role"] == "user"
    content_parts = user_msg["content"]
    assert isinstance(content_parts, list), "content should be a multi-part list"
    image_part = [p for p in content_parts if p.get("type") == "image_url"]
    assert image_part, f"expected image_url part in content, got types: {[p.get('type') for p in content_parts]}"
    assert image_part[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    # Verify response_format is set for JSON mode.
    rf = captured.get("response_format")
    assert rf == {"type": "json_object"}, f"expected json_object response_format, got {rf}"


def test_describe_photo_schema_conforming_response_parses():
    """Happy path: a schema-conforming wrapped response decodes the same as today."""
    content = '{"suggestions": [{"name": "Wallet", "category": "other", "confidence": 0.7}]}'
    assert _parse_suggestions(content) == [
        {"name": "Wallet", "category": "other", "confidence": 0.7}
    ]


def test_describe_photo_single_item_response_propagates(tmp_path, valid_jpeg_bytes, monkeypatch):
    """1-item response must propagate without being dropped."""
    captured: dict = {}
    _patch_openai(
        monkeypatch, captured,
        response_content='{"suggestions": [{"name": "Only Item"}]}',
    )

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    suggestions, _elapsed = describe_photo(
        photo_path, "http://fake:11434/v1", "test-key", "qwen3-vl:2b",
    )
    assert suggestions == [{"name": "Only Item"}]


def test_describe_photo_bare_list_response_still_parses(tmp_path, valid_jpeg_bytes, monkeypatch):
    """Defense-in-depth: if the server ignores the schema, Dev2_005's tolerant
    parser must still handle the bare-list fallback shape."""
    captured: dict = {}
    _patch_openai(
        monkeypatch, captured,
        response_content='[{"name": "Fallback Item", "category": "other"}]',
    )

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    suggestions, _elapsed = describe_photo(
        photo_path, "http://fake:11434/v1", "test-key", "qwen3-vl:2b",
    )
    assert suggestions == [{"name": "Fallback Item", "category": "other"}]


def test_describe_photo_error_returns_empty(tmp_path, valid_jpeg_bytes, monkeypatch):
    """Client errors must be caught gracefully and return ([], elapsed_ms)."""

    class _BoomClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise ConnectionError("network down")

        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(vision_mod, "openai", type("openai", (), {"OpenAI": _BoomClient}))

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    suggestions, elapsed = describe_photo(
        photo_path, "http://fake:11434/v1", "test-key", "qwen3-vl:2b",
    )
    assert suggestions == []
    assert elapsed >= 0


# ---------------------------------------------------------------------------
# Dev2_008: Bounding box support in /suggest response.
# bbox must be list[float] (normalized 0–1), passed through parser and
# describe_photo, and reflected in the Pydantic schema.
# ---------------------------------------------------------------------------


def test_parse_suggestions_bbox_passthrough():
    """bbox field must survive parsing and appear in the output dict."""
    raw = json.dumps({"suggestions": [
        {"name": "Resistor", "category": "electronics", "confidence": 0.8,
         "bbox": [0.1, 0.2, 0.5, 0.6]},
    ]})
    out = _parse_suggestions(raw)
    assert len(out) == 1
    assert out[0]["bbox"] == [0.1, 0.2, 0.5, 0.6]


def test_parse_suggestions_bbox_none_when_absent():
    """Items without bbox must still parse; bbox simply absent from dict."""
    raw = json.dumps({"suggestions": [
        {"name": "Bolt", "category": "fastener", "confidence": 0.9},
    ]})
    out = _parse_suggestions(raw)
    assert len(out) == 1
    assert "bbox" not in out[0] or out[0].get("bbox") is None


def test_describe_photo_bbox_passthrough(tmp_path, valid_jpeg_bytes, monkeypatch):
    """describe_photo must pass bbox floats through to the caller."""
    captured: dict = {}
    _patch_openai(
        monkeypatch, captured,
        response_content=json.dumps({"suggestions": [
            {"name": "Capacitor", "category": "electronics", "confidence": 0.7,
             "bbox": [0.12, 0.34, 0.56, 0.78]},
        ]}),
    )

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    suggestions, _elapsed = describe_photo(
        photo_path, "http://fake:11434/v1", "test-key", "qwen3-vl:2b",
    )
    assert len(suggestions) == 1
    assert suggestions[0]["bbox"] == [0.12, 0.34, 0.56, 0.78]


def test_describe_photo_mixed_bbox_and_no_bbox(tmp_path, valid_jpeg_bytes, monkeypatch):
    """Response with some items having bbox and others not must preserve both."""
    captured: dict = {}
    _patch_openai(
        monkeypatch, captured,
        response_content=json.dumps({"suggestions": [
            {"name": "Bolt", "bbox": [0.0, 0.0, 0.5, 0.5]},
            {"name": "Washer"},
        ]}),
    )

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    suggestions, _elapsed = describe_photo(
        photo_path, "http://fake:11434/v1", "test-key", "qwen3-vl:2b",
    )
    assert len(suggestions) == 2
    assert suggestions[0]["bbox"] == [0.0, 0.0, 0.5, 0.5]
    assert suggestions[1].get("bbox") is None


def test_suggested_item_bbox_accepts_floats():
    """SuggestedItem.bbox must accept float values (normalized 0–1 coords)."""
    from app.services.vision import SuggestedItem
    item = SuggestedItem(name="Screw", bbox=[0.1, 0.2, 0.3, 0.4])
    assert item.bbox == [0.1, 0.2, 0.3, 0.4]
    assert all(isinstance(v, float) for v in item.bbox)


def test_suggest_response_schema_bbox_is_number():
    """The JSON Schema for SuggestedItem.bbox items must be 'number' (not 'integer')
    so Ollama/Fireworks emit normalized 0–1 floats."""
    schema = vision_mod.SuggestResponseSchema.model_json_schema()
    item_schema = schema["properties"]["suggestions"]["items"]
    if "$ref" in item_schema:
        ref = item_schema["$ref"].rsplit("/", 1)[-1]
        item_schema = schema["$defs"][ref]
    bbox_prop = item_schema["properties"]["bbox"]
    # bbox is anyOf: [list-of-number, null]
    array_schema = next(
        s for s in bbox_prop.get("anyOf", [bbox_prop])
        if s.get("type") == "array"
    )
    assert array_schema["items"]["type"] == "number", (
        f"bbox items must be 'number' (float), got {array_schema['items']}"
    )


def test_prompt_requests_normalized_bbox():
    """_PROMPT must ask for normalized 0–1 coordinates, not pixel coords."""
    prompt = vision_mod._PROMPT
    assert "0" in prompt and "1" in prompt
    # Must NOT ask for pixel coordinates
    assert "pixel" not in prompt.lower()
