from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.request
from pathlib import Path

from PIL import Image

_PROMPT = (
    'Return ONLY valid JSON using the schema '
    '{"suggestions":[{"name":"string","category":"fastener|electronics|tool|label_packaging|other","confidence":0.0}]} '
    'List up to 5 likely item types visible. No explanation, no markdown.'
)


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
    ollama_url: str,
    model: str,
    max_px: int = 1280,
) -> tuple[list[dict], int]:
    """Use Ollama vision model to identify items in a photo.

    Returns (suggestions, elapsed_ms). On any failure returns ([], elapsed_ms).
    elapsed_ms covers the full Ollama round-trip.
    Image is downscaled to max_px on the longest side before sending.
    """
    try:
        image_bytes = _load_and_resize(photo_path, max_px)
    except (OSError, Exception):
        return [], 0

    image_b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": _PROMPT,
                "images": [image_b64],
            }
        ],
        "stream": False,
        "keep_alive": -1,
    }

    t0 = time.monotonic()
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{ollama_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read())
    except Exception:
        return [], int((time.monotonic() - t0) * 1000)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    try:
        content = body["message"]["content"]
    except (KeyError, TypeError):
        return [], elapsed_ms

    # Strip thinking tokens (qwen3 models)
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    # Strip markdown code fences
    content = re.sub(r"^```(?:json)?\s*", "", content).rstrip("` \n")

    try:
        parsed = json.loads(content)
        suggestions = [
            s for s in parsed.get("suggestions", [])
            if isinstance(s, dict) and s.get("name")
        ]
        return suggestions, elapsed_ms
    except (json.JSONDecodeError, AttributeError):
        return [], elapsed_ms
