"""Finding #24: tolerant /suggest parser + WARN logging on parse failure.

qwen3-vl:2b is non-deterministic about wrapping its response: sometimes the
expected ``{"suggestions": [...]}`` object, sometimes a bare JSON list. The
old parser called ``.get("suggestions", [])`` on the parsed value and silently
swallowed the resulting AttributeError, returning ``[]`` and losing the
training signal. These tests pin down the tolerant contract.
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
# Finding #27: Ollama structured outputs — format=<schema> wired into /suggest.
# ---------------------------------------------------------------------------


class _FakeOllamaResp:
    """Minimal urllib response shim that supports the `with urlopen(...)` idiom."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _write_jpeg(tmp_path, valid_jpeg_bytes):
    """Helper: drop a valid 1x1 JPEG onto disk and return the path."""
    p = tmp_path / "warm.jpg"
    p.write_bytes(valid_jpeg_bytes)
    return str(p)


def _patch_urlopen(monkeypatch, captured: dict, response_content: str):
    """Patch urllib.request.urlopen to capture the outgoing payload and return
    a canned Ollama /api/chat response whose message content is ``response_content``."""

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        body = json.dumps({"message": {"content": response_content}}).encode()
        return _FakeOllamaResp(body)

    # vision.py imports urllib.request; patch the module attribute it uses.
    import urllib.request as _urllib_request
    monkeypatch.setattr(_urllib_request, "urlopen", fake_urlopen)


def test_suggest_response_schema_shape():
    """The Pydantic schema must have a 'suggestions' array of name-bearing items.
    This guards the shape the Ollama server will be asked to enforce."""
    assert hasattr(vision_mod, "SuggestResponseSchema"), (
        "vision module must expose SuggestResponseSchema (Finding #27)"
    )
    schema = vision_mod.SuggestResponseSchema.model_json_schema()
    assert schema.get("type") == "object"
    props = schema.get("properties", {})
    assert "suggestions" in props, schema
    assert props["suggestions"]["type"] == "array"
    # The item schema must at minimum require a 'name' field.
    item_schema = props["suggestions"]["items"]
    # Pydantic uses $ref for nested models — resolve it.
    if "$ref" in item_schema:
        ref = item_schema["$ref"].rsplit("/", 1)[-1]
        item_schema = schema["$defs"][ref]
    assert "name" in item_schema.get("properties", {}), item_schema
    assert "name" in item_schema.get("required", []), item_schema


def test_describe_photo_sends_format_schema(tmp_path, valid_jpeg_bytes, monkeypatch):
    """describe_photo must include `format=<schema>` in the Ollama payload so
    the server enforces the wrapper shape on its end (Finding #27)."""
    captured: dict = {}
    _patch_urlopen(
        monkeypatch,
        captured,
        response_content='{"suggestions": [{"name": "M3 Bolt"}]}',
    )

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    suggestions, _elapsed = describe_photo(photo_path, "http://fake:11434", "qwen3-vl:2b")

    assert suggestions == [{"name": "M3 Bolt"}]
    body = captured["body"]
    assert "format" in body, f"Ollama payload missing 'format': {list(body.keys())}"
    fmt = body["format"]
    # Expect a JSON Schema object (dict), not the string "json" — strings don't
    # constrain shape, and Finding #27 is specifically about schema enforcement.
    assert isinstance(fmt, dict), f"format must be a JSON Schema dict, got {type(fmt).__name__}"
    assert fmt.get("type") == "object"
    assert "suggestions" in fmt.get("properties", {})


def test_describe_photo_schema_conforming_response_parses():
    """Happy path: a schema-conforming wrapped response decodes the same as today."""
    content = '{"suggestions": [{"name": "Wallet", "category": "other", "confidence": 0.7}]}'
    assert _parse_suggestions(content) == [
        {"name": "Wallet", "category": "other", "confidence": 0.7}
    ]


def test_describe_photo_single_item_response_propagates(tmp_path, valid_jpeg_bytes, monkeypatch):
    """Schema-enforced responses may have lower recall (Dev1_009 photo_id=15:
    4 items freeform → 1 with schema). The pipeline must still propagate a
    1-item response without dropping it."""
    captured: dict = {}
    _patch_urlopen(
        monkeypatch,
        captured,
        response_content='{"suggestions": [{"name": "Only Item"}]}',
    )

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    suggestions, _elapsed = describe_photo(photo_path, "http://fake:11434", "qwen3-vl:2b")

    assert suggestions == [{"name": "Only Item"}]


def test_describe_photo_bare_list_response_still_parses(tmp_path, valid_jpeg_bytes, monkeypatch):
    """Defense-in-depth: if the server ignores the schema (old Ollama, edge case),
    Dev2_005's tolerant parser must still handle the bare-list fallback shape."""
    captured: dict = {}
    _patch_urlopen(
        monkeypatch,
        captured,
        response_content='[{"name": "Fallback Item", "category": "other"}]',
    )

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    suggestions, _elapsed = describe_photo(photo_path, "http://fake:11434", "qwen3-vl:2b")

    assert suggestions == [{"name": "Fallback Item", "category": "other"}]
