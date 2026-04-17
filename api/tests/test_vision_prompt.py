"""Dev2_015: tighten the Fireworks VLM prompt for per-object bboxes.

Observed failure mode: Fireworks returned near-full-image bboxes (e.g.
[0.03, 0.11, 0.97, 0.9] → 94%×79% coverage), making iOS overlays invisible.
Root cause is prompt quality. These tests enforce the new prompt content and
the 'keep but warn' policy on suspicious bboxes.
"""
import json
import logging

import pytest

from app.services import vision as vision_mod
from app.services.vision import _PROMPT


def test_prompt_contains_tightness_instruction():
    """Prompt must tell the model to tightly enclose each object."""
    p = _PROMPT.lower()
    assert "tight" in p, "prompt must instruct model to tightly enclose objects"
    assert "whole image" in p or "entire image" in p, (
        "prompt must explicitly forbid whole-image bboxes"
    )


def test_prompt_contains_per_object_instruction():
    """Prompt must say 'one bbox per object' (not one per category)."""
    p = _PROMPT.lower()
    assert "per object" in p or "per instance" in p or "one bbox per" in p, (
        "prompt must instruct model to emit one bbox per visible instance"
    )


def test_prompt_contains_omission_fallback():
    """Prompt must instruct the model to OMIT items it can't localize
    instead of returning placeholder/whole-image boxes."""
    p = _PROMPT.lower()
    assert "omit" in p, "prompt must tell model to omit unlocalizable items"


def test_prompt_contains_few_shot_example():
    """A GOOD/BAD bbox example helps VLMs calibrate. Keep tokens minimal."""
    p = _PROMPT.lower()
    assert "good" in p and "bad" in p, (
        "prompt should include a minimal good-vs-bad bbox few-shot example"
    )


# Fixture helpers — mirror the style of test_vision.py
def _write_jpeg(tmp_path, valid_jpeg_bytes):
    p = tmp_path / "photo.jpg"
    p.write_bytes(valid_jpeg_bytes)
    return str(p)


def _patch_openai(monkeypatch, captured: dict, response_content: str):
    """Stub openai.OpenAI so describe_photo returns the injected content."""
    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Msg(content)

    class _Resp:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    class _Client:
        def __init__(self, **kw):
            captured["client_kw"] = kw
            self.chat = self
            self.completions = self

        def create(self, **kw):
            captured["create_kw"] = kw
            return _Resp(response_content)

    monkeypatch.setattr("app.services.vision.openai.OpenAI", _Client)


def test_describe_photo_warns_on_whole_image_bbox(tmp_path, valid_jpeg_bytes, monkeypatch, caplog):
    """Policy (Dev2_015): keep suspicious whole-image bboxes but log at WARN
    so the telemetry is visible. Do not silently drop — that hides bad prompt
    behaviour from the ops team."""
    captured: dict = {}
    _patch_openai(
        monkeypatch, captured,
        response_content=json.dumps({"suggestions": [
            {"name": "Stapler", "category": "tool", "confidence": 0.9,
             "bbox": [0.0, 0.0, 1.0, 1.0]},
        ]}),
    )

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    with caplog.at_level(logging.WARNING, logger="app.services.vision"):
        suggestions, _ = vision_mod.describe_photo(
            photo_path, "http://fake/v1", "k", "qwen3-vl:2b", photo_id=49,
        )

    # Keep the row (don't drop it).
    assert len(suggestions) == 1
    assert suggestions[0]["bbox"] == [0.0, 0.0, 1.0, 1.0]

    # And log a WARN citing photo_id + name + coverage fraction.
    messages = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any(
        "whole_image_bbox" in m and "photo_id=49" in m and "Stapler" in m
        for m in messages
    ), f"expected whole_image_bbox WARN with photo_id + name, got: {messages}"


def test_describe_photo_does_not_warn_on_tight_bbox(tmp_path, valid_jpeg_bytes, monkeypatch, caplog):
    captured: dict = {}
    _patch_openai(
        monkeypatch, captured,
        response_content=json.dumps({"suggestions": [
            {"name": "Stapler", "category": "tool", "confidence": 0.9,
             "bbox": [0.1, 0.2, 0.3, 0.4]},
        ]}),
    )

    photo_path = _write_jpeg(tmp_path, valid_jpeg_bytes)
    with caplog.at_level(logging.WARNING, logger="app.services.vision"):
        vision_mod.describe_photo(
            photo_path, "http://fake/v1", "k", "qwen3-vl:2b", photo_id=50,
        )

    assert not any(
        "whole_image_bbox" in rec.getMessage()
        for rec in caplog.records if rec.levelno >= logging.WARNING
    )
