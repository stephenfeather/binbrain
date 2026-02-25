from __future__ import annotations

import base64
import json
import re
import urllib.request
from pathlib import Path

_PROMPT = (
    'Return ONLY valid JSON using the schema '
    '{"suggestions":[{"name":"string","category":"fastener|electronics|tool|label_packaging|other","confidence":0.0}]} '
    'List up to 5 likely item types visible. No explanation, no markdown.'
)


def describe_photo(photo_path: str, ollama_url: str, model: str) -> list[dict]:
    """Use Ollama vision model to identify items in a photo. Returns [] on any failure."""
    try:
        image_bytes = Path(photo_path).read_bytes()
    except OSError:
        return []

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

    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{ollama_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except Exception:
        return []

    try:
        content = body["message"]["content"]
    except (KeyError, TypeError):
        return []

    # Strip thinking tokens (qwen3 models)
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    # Strip markdown code fences
    content = re.sub(r"^```(?:json)?\s*", "", content).rstrip("` \n")

    try:
        parsed = json.loads(content)
        return [
            s for s in parsed.get("suggestions", [])
            if isinstance(s, dict) and s.get("name")
        ]
    except (json.JSONDecodeError, AttributeError):
        return []
