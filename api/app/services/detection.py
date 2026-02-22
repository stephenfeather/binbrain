from __future__ import annotations

from typing import TypedDict


class Detection(TypedDict):
    label: str
    category: str | None
    confidence: float
    bbox: list[float]


def detect(photo_path: str) -> list[Detection]:
    """
    Detect objects in the given photo and return a list of detections.

    Expected detection schema:
      - label: str
      - category: str | None
      - confidence: float
      - bbox: [x1, y1, x2, y2]

    Stub implementation for now.
    """
    _ = photo_path
    return []
