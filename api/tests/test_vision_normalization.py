"""Dev2_015 iter 2: server-side pixel-to-normalized bbox safety net.

Iter 1 revealed Fireworks ignores the "0-1 coordinates" prompt instruction and
returns raw pixel values (e.g. [86, 138, 991, 997]). iOS clamps each coord to
[0,1] so every pixel value becomes 1 and overlays collapse to nothing. This
module enforces normalization on the server so iOS sees 0-1 floats no matter
what the vision model produces.
"""

import json
import logging

import pytest
from app.services import vision as vision_mod
from PIL import Image

# ---------------------------------------------------------------------------
# Helpers — mirror the stub pattern in test_vision.py and test_vision_prompt.py
# ---------------------------------------------------------------------------


def _make_jpeg(tmp_path, size: tuple[int, int]):
    """Write a JPEG of the requested dims so PIL.Image.open reports them."""
    p = tmp_path / f"photo_{size[0]}x{size[1]}.jpg"
    Image.new("RGB", size, "red").save(p, format="JPEG")
    return str(p)


def _patch_openai(monkeypatch, response_content: str):
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
            self.chat = self
            self.completions = self

        def create(self, **kw):
            return _Resp(response_content)

    monkeypatch.setattr("app.services.vision.openai.OpenAI", _Client)


# ---------------------------------------------------------------------------
# A. Normalization — the core iter 2 fix
# ---------------------------------------------------------------------------


def test_describe_photo_normalizes_pixel_space_bbox(tmp_path, monkeypatch, caplog):
    """The lamp-base incident: bbox=[86, 138, 991, 997] on a ~1024 image
    must come out as normalized 0-1 floats with clamping."""
    photo_path = _make_jpeg(tmp_path, (1024, 1024))
    _patch_openai(
        monkeypatch,
        json.dumps(
            {
                "suggestions": [
                    {
                        "name": "lamp base",
                        "category": "tool",
                        "confidence": 0.9,
                        "bbox": [86, 138, 991, 997],
                    },
                ]
            }
        ),
    )

    with caplog.at_level(logging.INFO, logger="app.services.vision"):
        hits, _ = vision_mod.describe_photo(
            photo_path,
            "http://fake/v1",
            "k",
            "qwen3p6-plus",
            max_px=2048,
            photo_id=1,
        )

    assert len(hits) == 1
    bbox = hits[0]["bbox"]
    assert bbox == pytest.approx([86 / 1024, 138 / 1024, 991 / 1024, 997 / 1024], abs=0.001)
    # INFO telemetry so the conversion is visible in prod logs.
    assert any(
        "vision.bbox.normalized" in r.getMessage() and "photo_id=1" in r.getMessage()
        for r in caplog.records
    ), f"expected vision.bbox.normalized INFO, got: {[r.getMessage() for r in caplog.records]}"


def test_describe_photo_normalizes_non_square_image(tmp_path, monkeypatch):
    """Rectangle images — divide by (w, h) not a single side."""
    photo_path = _make_jpeg(tmp_path, (1024, 768))
    _patch_openai(
        monkeypatch,
        json.dumps(
            {
                "suggestions": [
                    {"name": "Thing", "confidence": 0.8, "bbox": [100, 400, 800, 600]},
                ]
            }
        ),
    )

    hits, _ = vision_mod.describe_photo(
        photo_path,
        "http://fake/v1",
        "k",
        "qwen3p6-plus",
        max_px=2048,
        photo_id=2,
    )

    bbox = hits[0]["bbox"]
    assert bbox == pytest.approx([100 / 1024, 400 / 768, 800 / 1024, 600 / 768], abs=0.001)


def test_describe_photo_accepts_already_normalized_bbox(tmp_path, monkeypatch, caplog):
    """Values all <= 1.5 pass through untouched — no normalization telemetry."""
    photo_path = _make_jpeg(tmp_path, (1024, 1024))
    _patch_openai(
        monkeypatch,
        json.dumps(
            {
                "suggestions": [
                    {"name": "Thing", "confidence": 0.9, "bbox": [0.1, 0.2, 0.5, 0.6]},
                ]
            }
        ),
    )

    with caplog.at_level(logging.INFO, logger="app.services.vision"):
        hits, _ = vision_mod.describe_photo(
            photo_path,
            "http://fake/v1",
            "k",
            "qwen3p6-plus",
            max_px=2048,
            photo_id=3,
        )

    assert hits[0]["bbox"] == [0.1, 0.2, 0.5, 0.6]
    # Already normalized — do not log conversion.
    assert not any("vision.bbox.normalized" in r.getMessage() for r in caplog.records)


def test_describe_photo_clamps_out_of_range_pixel_bbox(tmp_path, monkeypatch):
    """Defensive: pixel coords that exceed image dims (bad model output)
    should clamp to [0,1] rather than emit values > 1."""
    photo_path = _make_jpeg(tmp_path, (1024, 1024))
    _patch_openai(
        monkeypatch,
        json.dumps(
            {
                "suggestions": [
                    {"name": "Thing", "confidence": 0.8, "bbox": [-10, 0, 2048, 4096]},
                ]
            }
        ),
    )

    hits, _ = vision_mod.describe_photo(
        photo_path,
        "http://fake/v1",
        "k",
        "qwen3p6-plus",
        max_px=2048,
        photo_id=4,
    )

    bbox = hits[0]["bbox"]
    assert all(0.0 <= v <= 1.0 for v in bbox), f"expected clamped bbox, got {bbox}"


# ---------------------------------------------------------------------------
# whole_image_bbox coverage must now be computed on NORMALIZED coords
# ---------------------------------------------------------------------------


def test_whole_image_warn_uses_normalized_coverage(tmp_path, monkeypatch, caplog):
    """Iter 1 logged coverage=777395 because it multiplied pixel values.
    After normalization, coverage is a real 0-1 fraction."""
    photo_path = _make_jpeg(tmp_path, (1024, 1024))
    _patch_openai(
        monkeypatch,
        json.dumps(
            {
                "suggestions": [
                    {"name": "lamp base", "confidence": 0.9, "bbox": [86, 138, 991, 997]},
                ]
            }
        ),
    )

    with caplog.at_level(logging.WARNING, logger="app.services.vision"):
        vision_mod.describe_photo(
            photo_path,
            "http://fake/v1",
            "k",
            "qwen3p6-plus",
            max_px=2048,
            photo_id=5,
        )

    whole_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "whole_image_bbox" in r.getMessage()
    ]
    assert len(whole_msgs) == 1
    # 0.884 * 0.839 ≈ 0.742 — a real fraction, not 777395.
    assert (
        "coverage=0." in whole_msgs[0]
    ), f"coverage must be a decimal fraction, got: {whole_msgs[0]}"


# ---------------------------------------------------------------------------
# B. Prompt strengthening
# ---------------------------------------------------------------------------


def test_prompt_contains_pixel_rejection():
    p = vision_mod._PROMPT.lower()
    assert "pixel" in p, "prompt must name pixel coords as forbidden"
    assert "0.0" in p and "1.0" in p, "prompt must state the [0.0, 1.0] range explicitly"


def test_prompt_contains_valid_invalid_examples():
    p = vision_mod._PROMPT.lower()
    assert "valid" in p and "invalid" in p, "prompt must contrast VALID vs INVALID"


def test_prompt_contains_empty_list_rejection_clause():
    p = vision_mod._PROMPT.lower()
    # "suggestions: []" or "suggestions":[] or "empty list" — any clear indicator.
    assert (
        "suggestions: []" in p or 'suggestions":[]' in p or "empty list" in p
    ), "prompt must tell model to return suggestions: [] when localization fails"
