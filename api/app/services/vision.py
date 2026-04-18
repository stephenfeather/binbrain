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

# Dev2_016: manual prompt-version stamp. Bumped by hand whenever `_PROMPT`
# below is materially changed. Persisted on every photo_detections row written
# by the /suggest path so "did prompt v2 beat v1?" is answerable from data.
# Explicit > hashing: keeps the provenance readable in the DB.
# Public (no leading underscore) because it's imported by app.routes.photos.
PROMPT_VERSION = "v2"

# Dev2_015 iter 2: prior iter 1 prompt was not followed by Fireworks — logs
# showed bbox=[86,138,991,997] (pixels, not 0-1) and one suggestion per photo.
# This version states the bbox coordinate range TWICE (top and bottom), gives
# explicit VALID-vs-INVALID numeric examples, and a concrete multi-item example
# so the model cannot collapse to a single whole-image box. Strict JSON-only
# output contract and existing category vocabulary preserved. A server-side
# normalization safety net (`_normalize_bboxes`) remains in place in case the
# model still ignores these instructions.
_PROMPT = (
    "RULE: Every bbox value must be a decimal between 0.0 and 1.0 inclusive. "
    "Do NOT use pixel coordinates under any circumstances. "
    "Return ONLY valid JSON using the schema "
    '{"suggestions":[{"name":"string","category":"fastener|electronics|tool|label_packaging|other","confidence":0.0,"bbox":[x1,y1,x2,y2]}]} '
    "Coordinates are normalized 0-1 with the top-left origin. "
    "Each bbox MUST tightly enclose ONLY the named object, cropped to its visible extent. "
    "Do NOT return the whole image as a bbox. A single object's bbox should almost never exceed 50% of the image area "
    "(unless it is a single large surface — e.g. a wall or floor — that fills the frame). "
    "VALID bbox: [0.05, 0.30, 0.45, 0.80] (all values in [0, 1]). "
    "INVALID bbox: [50, 300, 450, 800] (these are pixel values — REJECT and REDO). "
    "Emit one bbox per visible instance: if you see a stapler AND a mug, return TWO suggestions: "
    '[{"name":"stapler","bbox":[0.1,0.4,0.4,0.75]},{"name":"mug","bbox":[0.55,0.2,0.85,0.6]}]. '
    "Do NOT merge distinct objects into a single whole-image bbox. "
    'If you cannot precisely localize distinct objects, return an empty list "suggestions": [] — '
    "do NOT return approximate, placeholder, or whole-image boxes. OMIT items you cannot localize. "
    "FINAL REMINDER: each bbox coordinate must be a decimal between 0.0 and 1.0. "
    "No explanation, no markdown."
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
    bbox: list[float] | None = None


class SuggestResponseSchema(BaseModel):
    """Wrapper schema handed to Ollama as ``format=<schema>`` so the server
    enforces the ``{"suggestions": [...]}`` envelope on its end (Finding #27).

    Dev1_009 confirmed qwen3-vl:2b honors this: 6/6 calls produced conforming
    JSON. The defensive parser in :func:`_parse_suggestions` is retained as
    belt-and-suspenders for older Ollama builds or models that ignore ``format``.
    """

    suggestions: list[SuggestedItem]


_SUGGEST_SCHEMA: dict = SuggestResponseSchema.model_json_schema()


# Dev2_015: bbox coverage above this fraction of the image area is suspicious
# (treated as "model gave up and returned a whole-image box"). Kept for
# telemetry only — we do not drop the row, per policy in the task prompt.
_WHOLE_IMAGE_BBOX_COVERAGE_THRESHOLD = 0.7

# Dev2_015 iter 2: any bbox component strictly above this threshold is treated
# as pixel-space and gets normalized against the image dimensions. Normalized
# coords are in [0, 1]; a value like 1.05 is a rounding artefact, 1.5 cleanly
# separates that from "definitely pixels" (e.g. 86 or 991).
_PIXEL_BBOX_THRESHOLD = 1.5


def _normalize_bboxes(
    suggestions: list[dict],
    image_dims: tuple[int, int] | None,
    photo_id: int | None,
    flags_out: dict | None = None,
) -> None:
    """Convert pixel-space bboxes to normalized 0-1 floats in place.

    Fireworks (observed with qwen3p6-plus, Dev2_015 iter 1) ignores the prompt
    instruction and returns raw pixel values like ``[86, 138, 991, 997]``. iOS
    clamps each coord to [0, 1] so every pixel value becomes 1 and overlays
    collapse to nothing. This helper detects pixel-space bboxes (any component
    > 1.5) and divides by the image dimensions, clamping into [0, 1].

    No-op when ``image_dims`` is ``None`` (image failed to open earlier) or
    when every value is already within range.
    """
    if image_dims is None:
        return
    width, height = image_dims
    if width <= 0 or height <= 0:
        return

    for s in suggestions:
        bbox = s.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            continue
        if max(abs(x1), abs(y1), abs(x2), abs(y2)) <= _PIXEL_BBOX_THRESHOLD:
            continue  # already normalized

        original = [x1, y1, x2, y2]
        nx1 = min(1.0, max(0.0, x1 / width))
        ny1 = min(1.0, max(0.0, y1 / height))
        nx2 = min(1.0, max(0.0, x2 / width))
        ny2 = min(1.0, max(0.0, y2 / height))
        s["bbox"] = [nx1, ny1, nx2, ny2]
        if flags_out is not None:
            flags_out["bbox_normalized"] = True

        logger.info(
            "event=vision.bbox.normalized photo_id=%s name=%s from_pixel=%s "
            "to_normalized=[%.3f,%.3f,%.3f,%.3f] image_size=(%d,%d)",
            photo_id,
            s.get("name"),
            original,
            nx1,
            ny1,
            nx2,
            ny2,
            width,
            height,
        )


def _log_suspicious_bboxes(
    suggestions: list[dict],
    photo_id: int | None,
    flags_out: dict | None = None,
) -> None:
    """Emit a WARN line for each suggestion whose (normalized) bbox covers
    ≥70% of the image. Expects bboxes to already be in [0, 1] — call
    :func:`_normalize_bboxes` first.

    Does NOT mutate the list — the row is kept so downstream clients see
    exactly what the model returned, but the telemetry is loud enough that
    prompt-regression pagers fire before any user complaint does. When
    ``flags_out`` is supplied, ``flags_out["whole_image_bbox_warn"] = True``
    is set whenever at least one suspicious bbox fires — making the signal
    queryable via ``vision_calls.flags`` instead of log grep.
    """
    for s in suggestions:
        bbox = s.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            continue
        coverage = max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))
        if coverage >= _WHOLE_IMAGE_BBOX_COVERAGE_THRESHOLD:
            if flags_out is not None:
                flags_out["whole_image_bbox_warn"] = True
            logger.warning(
                "event=vision.suggest.whole_image_bbox photo_id=%s name=%s coverage=%.3f bbox=%s",
                photo_id,
                s.get("name"),
                coverage,
                bbox,
            )


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
                photo_id,
                list(parsed.keys()),
                sample,
            )
            return []
    elif isinstance(parsed, list):
        items = parsed
    else:
        logger.warning(
            "event=vision_parse_failed reason=unexpected_type photo_id=%s type=%s sample=%r",
            photo_id,
            type(parsed).__name__,
            sample,
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
            photo_id,
            exc.__class__.__name__,
            content[:200],
        )
        return []

    return _extract_suggestions(parsed, photo_id, content[:200])


def _load_and_resize(photo_path: str, max_px: int) -> tuple[bytes, tuple[int, int]]:
    """Load image and downscale so the longest side is at most ``max_px``.

    Returns ``(jpeg_bytes, (w, h))`` where the dims reflect the *post-resize*
    size — i.e. the coordinate space the vision model sees. Downstream bbox
    normalization must divide by THESE dims (not the original) so ratios are
    correct. Never upscales. Raises ``OSError`` if the file can't be read.
    """
    with Image.open(photo_path) as img:
        img = img.convert("RGB")
        if max(img.width, img.height) > max_px:
            img.thumbnail((max_px, max_px), Image.LANCZOS)
        dims = (img.width, img.height)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), dims


def describe_photo(
    photo_path: str,
    base_url: str,
    api_key: str,
    model: str,
    max_px: int = 1280,
    photo_id: int | None = None,
    flags_out: dict | None = None,
) -> tuple[list[dict], int]:
    """Use a vision-language model to identify items in a photo.

    Communicates via the OpenAI-compatible chat completions API, making the
    backend swappable between Ollama (local) and hosted providers (e.g.
    Fireworks.ai) by changing ``base_url`` and ``api_key``.

    Returns (suggestions, elapsed_ms). On any failure returns ([], elapsed_ms).
    Image is downscaled to max_px on the longest side before sending.

    Optional ``flags_out`` is a mutable dict bag into which anomaly markers
    are recorded for downstream ops-instrumentation (Dev2_018). Known keys
    written here: ``bbox_normalized`` (pixel-space bbox was rescaled to 0-1)
    and ``whole_image_bbox_warn`` (at least one suggestion covered ≥70% of
    the image). The route layer mirrors this dict into ``vision_calls.flags``.
    """
    try:
        image_bytes, image_dims = _load_and_resize(photo_path, max_px)
    except (OSError, Exception):
        return [], 0

    image_b64 = base64.b64encode(image_bytes).decode()
    data_uri = f"data:image/jpeg;base64,{image_b64}"

    client = openai.OpenAI(base_url=base_url, api_key=api_key)
    logger.info(
        "event=vision_request_start photo_id=%s base_url=%s model=%s", photo_id, base_url, model
    )

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
        logger.warning(
            "event=vision_request_failed photo_id=%s base_url=%s model=%s ms=%s exc=%s",
            photo_id,
            base_url,
            model,
            elapsed_ms,
            exc.__class__.__name__,
        )
        return [], elapsed_ms
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    try:
        content = resp.choices[0].message.content
    except (IndexError, AttributeError):
        return [], elapsed_ms

    logger.info(
        "event=vision_response photo_id=%s base_url=%s model=%s ms=%s",
        photo_id,
        base_url,
        model,
        elapsed_ms,
    )
    logger.debug(
        "event=vision_response_raw photo_id=%s content=%r",
        photo_id,
        content[:500] if content else None,
    )

    suggestions = _parse_suggestions(content, photo_id)
    # Dev2_015 iter 2: normalize pixel coords BEFORE the whole-image check.
    # Fireworks ignores the prompt's 0-1 instruction and emits raw pixels;
    # iOS clamps to [0,1] so pixel values render as invisible overlays.
    _normalize_bboxes(suggestions, image_dims, photo_id, flags_out=flags_out)
    _log_suspicious_bboxes(suggestions, photo_id, flags_out=flags_out)
    return suggestions, elapsed_ms
