from __future__ import annotations

import base64
import json
import re
import time
import urllib.request
from pathlib import Path

_PROMPT = (
    'Return ONLY valid JSON using the schema '
    '{"suggestions":[{"name":"string","category":"fastener|electronics|tool|label_packaging|other","confidence":0.0}]} '
    'List up to 5 likely item types visible. No explanation, no markdown.'
)


def describe_photo(photo_path: str, ollama_url: str, model: str) -> tuple[list[dict], int]:
    """Use Ollama vision model to identify items in a photo.

    Returns (suggestions, elapsed_ms). On any failure returns ([], elapsed_ms).
    elapsed_ms covers the full Ollama round-trip.
    """
    try:
        image_bytes = Path(photo_path).read_bytes()
    except OSError:
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
    }

    t0 = time.monotonic()
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{ollama_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
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
