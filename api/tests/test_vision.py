"""Finding #24: tolerant /suggest parser + WARN logging on parse failure.

qwen3-vl:2b is non-deterministic about wrapping its response: sometimes the
expected ``{"suggestions": [...]}`` object, sometimes a bare JSON list. The
old parser called ``.get("suggestions", [])`` on the parsed value and silently
swallowed the resulting AttributeError, returning ``[]`` and losing the
training signal. These tests pin down the tolerant contract.
"""
from __future__ import annotations

import logging

from app.services.vision import _parse_suggestions


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
