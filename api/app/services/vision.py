from __future__ import annotations

import base64
import io
import json
import logging
import re
import time

import openai
from PIL import Image
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_PROMPT = (
    'Return ONLY valid JSON using the schema '
    '{"suggestions":[{"name":"string","category":"fastener|electronics|tool|label_packaging|other","confidence":0.0,"bbox":[x1,y1,x2,y2]}]} '
    'List all item types visible. For each item include a bbox with pixel coordinates [x1,y1,x2,y2] of the bounding box. No explanation, no markdown.'
)


class SuggestedItem(BaseModel):
    """Raw vision-model suggestion for a single item in a photo.

    Fields mirror the ``SuggestionItem`` schema in ``docs/openapi.yaml``,
    limited to what the vision model actually produces. Route-level
    enrichment (``bins``, ``match``) is attached downstream and is NOT
    part of the Ollama output contract.
    """

    name: str
    category: str | None = None
    confidence: float | None = None
    bbox: list[int] | None = None


class SuggestResponseSchema(BaseModel):
    """Wrapper schema handed to Ollama as ``format=<schema>`` so the server
    enforces the ``{"suggestions": [...]}`` envelope on its end (Finding #27).

    Dev1_009 confirmed qwen3-vl:2b honors this: 6/6 calls produced conforming
    JSON. The defensive parser in :func:`_parse_suggestions` is retained as
    belt-and-suspenders for older Ollama builds or models that ignore ``format``.
    """

    suggestions: list[SuggestedItem]


_SUGGEST_SCHEMA: dict = SuggestResponseSchema.model_json_schema()


def _extract_suggestions(parsed: object, photo_id: int | None, sample: str) -> list[dict]:
    """Pull the suggestions list out of any accepted shape.

    Accepts ``{"suggestions": [...]}`` (requested schema) or a bare list
    ``[...]`` (qwen3-vl:2b's alternate emission). Logs WARN and returns
    ``[]`` for any other shape so schema drift is debuggable. Items are
    filtered to dicts with a truthy 'name'.
    """
    if isinstance(parsed, dict):
        items = parsed.get("suggestions")
        if not isinstance(items, list):
            logger.warning(
                "event=vision_parse_failed reason=missing_suggestions_key photo_id=%s keys=%s sample=%r",
                photo_id, list(parsed.keys()), sample,
            )
            return []
    elif isinstance(parsed, list):
        items = parsed
    else:
        logger.warning(
            "event=vision_parse_failed reason=unexpected_type photo_id=%s type=%s sample=%r",
            photo_id, type(parsed).__name__, sample,
        )
        return []

    return [s for s in items if isinstance(s, dict) and s.get("name")]


def _parse_suggestions(raw_content: str, photo_id: int | None = None) -> list[dict]:
    """Sanitize + json-parse vision-model content, then extract suggestions.

    qwen3-vl:2b is non-deterministic about wrapping its response; the
    extractor handles both ``{"suggestions": [...]}`` and bare-list shapes.
    JSON parse failures are logged at WARN with photo_id and a payload
    sample so that schema drift is debuggable rather than silently swallowed.
    """
    content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
    content = re.sub(r"^```(?:json)?\s*", "", content).rstrip("` \n")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning(
            "event=vision_parse_failed reason=json_decode photo_id=%s exc=%s sample=%r",
            photo_id, exc.__class__.__name__, content[:200],
        )
        return []

    return _extract_suggestions(parsed, photo_id, content[:200])


def _load_and_resize(photo_path: str, max_px: int) -> bytes:
    """Load image and downscale so the longest side is at most max_px.

    Returns JPEG bytes. Never upscales. Raises OSError if the file can't be read.
    """
    with Image.open(photo_path) as img:
        img = img.convert("RGB")
        if max(img.width, img.height) > max_px:
            img.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


def describe_photo(
    photo_path: str,
    base_url: str,
    api_key: str,
    model: str,
    max_px: int = 1280,
    photo_id: int | None = None,
) -> tuple[list[dict], int]:
    """Use a vision-language model to identify items in a photo.

    Communicates via the OpenAI-compatible chat completions API, making the
    backend swappable between Ollama (local) and hosted providers (e.g.
    Fireworks.ai) by changing ``base_url`` and ``api_key``.

    Returns (suggestions, elapsed_ms). On any failure returns ([], elapsed_ms).
    Image is downscaled to max_px on the longest side before sending.
    """
    try:
        image_bytes = _load_and_resize(photo_path, max_px)
    except (OSError, Exception):
        return [], 0

    image_b64 = base64.b64encode(image_bytes).decode()
    data_uri = f"data:image/jpeg;base64,{image_b64}"

    client = openai.OpenAI(base_url=base_url, api_key=api_key)
    logger.info("event=vision_request_start photo_id=%s base_url=%s model=%s", photo_id, base_url, model)

    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            response_format={"type": "json_object"},
            timeout=180,
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.warning("event=vision_request_failed photo_id=%s base_url=%s model=%s ms=%s exc=%s", photo_id, base_url, model, elapsed_ms, exc.__class__.__name__)
        return [], elapsed_ms
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    try:
        content = resp.choices[0].message.content
    except (IndexError, AttributeError):
        return [], elapsed_ms

    logger.info("event=vision_response photo_id=%s base_url=%s model=%s ms=%s", photo_id, base_url, model, elapsed_ms)
    logger.debug("event=vision_response_raw photo_id=%s content=%r", photo_id, content[:500] if content else None)

    return _parse_suggestions(content, photo_id), elapsed_ms
