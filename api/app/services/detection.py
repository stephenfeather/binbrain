from __future__ import annotations

import logging
import platform
import threading
import time
import traceback
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
_deferred_classes: list[str] | None = None

# Finding #3: surface reload failures. Populated by `reload_classes`; read by
# `get_reload_status()` and exposed via /health. `status` is one of
# "never" | "ok" | "error". `error` carries the short message when status=error.
_reload_status: dict = {
    "status": "never",
    "last_reload_at": None,
    "count": 0,
    "error": None,
}
_reload_status_lock = threading.Lock()


def _record_reload_status(status: str, count: int, error: str | None) -> None:
    with _reload_status_lock:
        _reload_status["status"] = status
        _reload_status["last_reload_at"] = time.time()
        _reload_status["count"] = count
        _reload_status["error"] = error


def get_reload_status() -> dict:
    """Snapshot of the last reload attempt for ops/health visibility."""
    with _reload_status_lock:
        return dict(_reload_status)


def set_deferred_classes(classes: list[str]) -> None:
    """Store classes for deferred initialization on first model use."""
    global _deferred_classes
    _deferred_classes = classes
    logger.info("event=yoloe_deferred_classes count=%d", len(classes))


def _get_model():
    """Lazy-load YOLOE model, reloading if the configured model changes."""
    global _model, _model_path, _deferred_classes
    from app.deps import get_detection_model
    from ultralytics import YOLOE

    desired_path = get_detection_model()
    if _model is None or _model_path != desired_path:
        logger.info("event=yoloe_load model=%s", desired_path)
        _model = YOLOE(desired_path)
        _model_path = desired_path
        # Apply deferred classes on first load
        if _deferred_classes:
            logger.info("event=yoloe_set_classes count=%d", len(_deferred_classes))
            _model.set_classes(_deferred_classes)
            _deferred_classes = None
    return _model


def initialize_model(classes: list[str]) -> None:
    """Load the model and set initial classes. Called on startup."""
    with _model_lock:
        model = _get_model()
        if classes:
            logger.info("event=yoloe_set_classes count=%d", len(classes))
            model.set_classes(classes)


def reload_classes(classes: list[str]) -> None:
    """Re-encode text embeddings for the new class list.

    Called from a background thread by class_registry on mutations. Never
    raises: background-thread exceptions previously vanished silently while
    the HTTP request had already returned 200 (Finding #3). Failures are
    captured in `get_reload_status()` and logged at ERROR.
    """
    count = len(classes)
    try:
        with _model_lock:
            model = _get_model()
            if classes:
                logger.warning("event=yoloe_reload_start count=%d", count)
                model.set_classes(classes)
                logger.warning("event=yoloe_reload_done count=%d", count)
            else:
                logger.warning("event=yoloe_reload_empty")
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        logger.error(
            "event=yoloe_reload_failed count=%d error=%s\n%s",
            count,
            msg,
            traceback.format_exc(),
        )
        _record_reload_status("error", count, msg)
        return
    _record_reload_status("ok", count, None)


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
    """Detect objects in the given photo using YOLOE.

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

            detections.append(
                Detection(
                    label=label,
                    category=class_registry.get_category(label),
                    confidence=round(conf, 3),
                    bbox=[round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                )
            )

    return detections
