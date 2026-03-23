from __future__ import annotations

import logging
import platform
import threading
from typing import TypedDict

logger = logging.getLogger("binbrain")


class Detection(TypedDict):
    label: str
    category: str | None
    confidence: float
    bbox: list[float]


_model = None
_model_path: str | None = None
_model_lock = threading.RLock()


def _get_model():
    """Lazy-load YOLOWorld model, reloading if the configured model changes."""
    global _model, _model_path
    from ultralytics import YOLOWorld
    from app.deps import get_detection_model

    desired_path = get_detection_model()
    if _model is None or _model_path != desired_path:
        logger.info("event=yolo_world_load model=%s", desired_path)
        _model = YOLOWorld(desired_path)
        _model_path = desired_path
    return _model


def initialize_model(classes: list[str]) -> None:
    """Load the model and set initial classes. Called on startup."""
    with _model_lock:
        model = _get_model()
        if classes:
            logger.info("event=yolo_world_set_classes count=%d", len(classes))
            model.set_classes(classes)


def reload_classes(classes: list[str]) -> None:
    """Re-encode CLIP text embeddings for the new class list.

    Called from a background thread by class_registry on mutations.
    """
    with _model_lock:
        model = _get_model()
        if classes:
            logger.warning("event=yolo_world_reload_start count=%d", len(classes))
            model.set_classes(classes)
            logger.warning("event=yolo_world_reload_done count=%d", len(classes))
        else:
            logger.warning("event=yolo_world_reload_empty")


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
    """Detect objects in the given photo using YOLO-World.

    Returns a list of detections with label, category, confidence, and
    bounding box in [x1, y1, x2, y2] pixel coordinates.
    """
    from app.deps import get_yolo_world_conf
    from app.services import class_registry

    with _model_lock:
        model = _get_model()
        results = model.predict(
            source=photo_path,
            device=get_device(),
            conf=get_yolo_world_conf(),
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
                category=class_registry.get_category(label),
                confidence=round(conf, 3),
                bbox=[round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            ))

    return detections
