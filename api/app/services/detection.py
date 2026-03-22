from __future__ import annotations

import logging
import platform
from typing import TypedDict

logger = logging.getLogger("binbrain")

class Detection(TypedDict):
    label: str
    category: str | None
    confidence: float
    bbox: list[float]


# Map COCO class names to broader categories useful for bin inventory
COCO_CATEGORIES: dict[str, str] = {
    # Tools
    "scissors": "tools",
    "knife": "tools",
    # Electronics
    "cell phone": "electronics",
    "laptop": "electronics",
    "remote": "electronics",
    "keyboard": "electronics",
    "mouse": "electronics",
    "tv": "electronics",
    # Containers / household
    "bottle": "household",
    "cup": "household",
    "bowl": "household",
    "vase": "household",
    "clock": "household",
    # Sports / outdoor
    "sports ball": "sports",
    "tennis racket": "sports",
    "baseball bat": "sports",
    "baseball glove": "sports",
    "skateboard": "sports",
    "surfboard": "sports",
    "frisbee": "sports",
    "skis": "sports",
    "snowboard": "sports",
    "kite": "sports",
    # Books / office
    "book": "office",
    "backpack": "other",
    "umbrella": "other",
    "handbag": "other",
    "suitcase": "other",
    "tie": "other",
    # Food (less relevant but present in COCO)
    "banana": "food",
    "apple": "food",
    "sandwich": "food",
    "orange": "food",
    "broccoli": "food",
    "carrot": "food",
    "hot dog": "food",
    "pizza": "food",
    "donut": "food",
    "cake": "food",
}

_model = None
_model_path: str | None = None


def _get_model():
    """Lazy-load YOLO model, reloading if the configured model changes."""
    global _model, _model_path
    from ultralytics import YOLO
    from app.deps import get_detection_model

    desired_path = get_detection_model()
    if _model is None or _model_path != desired_path:
        logger.info("event=yolo_load model=%s", desired_path)
        _model = YOLO(desired_path)
        _model_path = desired_path
    return _model


def get_device() -> str:
    """Select best available device: MPS on Apple Silicon, CPU otherwise."""
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "mps"
    return "cpu"


def get_model_name() -> str:
    """Return the current detection model name for metadata."""
    from app.deps import get_detection_model
    return get_detection_model()


def detect(photo_path: str) -> list[Detection]:
    """Detect objects in the given photo using YOLO11s.

    Returns a list of detections with label, category, confidence, and
    bounding box in [x1, y1, x2, y2] pixel coordinates.
    """
    model = _get_model()
    results = model.predict(
        source=photo_path,
        device=get_device(),
        conf=0.25,
        iou=0.45,
        verbose=False,
    )

    detections: list[Detection] = []
    for result in results:
        boxes = result.boxes
        if boxes is None:
            continue
        for i in range(len(boxes)):
            cls_id = int(boxes.cls[i])
            label = model.names[cls_id]
            conf = float(boxes.conf[i])
            x1, y1, x2, y2 = boxes.xyxy[i].tolist()

            detections.append(Detection(
                label=label,
                category=COCO_CATEGORIES.get(label),
                confidence=round(conf, 3),
                bbox=[round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            ))

    return detections
