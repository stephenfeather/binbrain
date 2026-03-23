"""Unit tests for the detection service (no DB or YOLO weights required)."""
from __future__ import annotations

import sys
import types
import platform
from unittest.mock import MagicMock, patch

# Stub app.deps before importing detection, since deps requires DATABASE_URL
_mock_deps = types.ModuleType("app.deps")
_mock_deps.get_detection_model = lambda: "yolov8s-worldv2.pt"
_mock_deps.get_yolo_world_conf = lambda: 0.15

# Stub class_registry
_mock_registry = types.ModuleType("app.services.class_registry")
_mock_registry.get_category = lambda name: None
_mock_registry.get_classes = lambda: []

# Only stub if not already loaded (integration tests load the real ones)
if "app.deps" not in sys.modules:
    sys.modules.setdefault("app.deps", _mock_deps)
if "app.services.class_registry" not in sys.modules:
    sys.modules.setdefault("app.services.class_registry", _mock_registry)

from app.services.detection import (
    detect,
    get_device,
    get_model_name,
)


class TestGetDevice:
    def test_returns_mps_on_apple_silicon(self):
        with (
            patch.object(platform, "system", return_value="Darwin"),
            patch.object(platform, "machine", return_value="arm64"),
        ):
            assert get_device() == "mps"

    def test_returns_cpu_on_linux(self):
        with (
            patch.object(platform, "system", return_value="Linux"),
            patch.object(platform, "machine", return_value="x86_64"),
        ):
            assert get_device() == "cpu"

    def test_returns_cpu_on_intel_mac(self):
        with (
            patch.object(platform, "system", return_value="Darwin"),
            patch.object(platform, "machine", return_value="x86_64"),
        ):
            assert get_device() == "cpu"


class TestGetModelName:
    def test_returns_configured_model(self):
        with patch.object(_mock_deps, "get_detection_model", return_value="yolov8s-worldv2.pt"):
            assert get_model_name() == "yolov8s-worldv2.pt"


class TestDetect:
    def _make_mock_result(self, detections: list[dict]) -> MagicMock:
        """Build a mock ultralytics Result with boxes."""
        boxes = MagicMock()
        boxes.__len__ = lambda self: len(detections)

        cls_list = [d["cls"] for d in detections]
        conf_list = [d["conf"] for d in detections]

        class FakeXYXY:
            def __init__(self, coords):
                self._coords = coords
            def tolist(self):
                return self._coords

        xyxy_list = [FakeXYXY(d["xyxy"]) for d in detections]

        boxes.cls = cls_list
        boxes.conf = conf_list
        boxes.xyxy = xyxy_list

        result = MagicMock()
        result.boxes = boxes
        return result

    @patch("app.services.detection._get_model")
    def test_detect_returns_detections(self, mock_get_model):
        mock_model = MagicMock()
        mock_model.names = {0: "scissors", 1: "bottle"}
        mock_model.predict.return_value = [
            self._make_mock_result([
                {"cls": 0, "conf": 0.92, "xyxy": [10.0, 20.0, 100.0, 200.0]},
                {"cls": 1, "conf": 0.85, "xyxy": [150.0, 50.0, 300.0, 250.0]},
            ])
        ]
        mock_get_model.return_value = mock_model

        category_map = {"scissors": "tools", "bottle": "household"}
        _mock_deps.get_yolo_world_conf = lambda: 0.15

        # Patch the real class_registry module (may have replaced the stub)
        cr_mod = sys.modules.get("app.services.class_registry", _mock_registry)
        orig = getattr(cr_mod, "get_category", None)
        cr_mod.get_category = lambda name: category_map.get(name)
        try:
            results = detect("/fake/photo.jpg")
        finally:
            if orig is not None:
                cr_mod.get_category = orig

        assert len(results) == 2
        assert results[0]["label"] == "scissors"
        assert results[0]["category"] == "tools"
        assert results[0]["confidence"] == 0.92
        assert results[0]["bbox"] == [10.0, 20.0, 100.0, 200.0]
        assert results[1]["label"] == "bottle"
        assert results[1]["category"] == "household"

    @patch("app.services.detection._get_model")
    def test_detect_empty_photo(self, mock_get_model):
        mock_model = MagicMock()
        result = MagicMock()
        result.boxes = None
        mock_model.predict.return_value = [result]
        mock_get_model.return_value = mock_model

        _mock_deps.get_yolo_world_conf = lambda: 0.15

        results = detect("/fake/empty.jpg")
        assert results == []

    @patch("app.services.detection._get_model")
    def test_detect_unknown_category(self, mock_get_model):
        mock_model = MagicMock()
        mock_model.names = {0: "person"}
        mock_model.predict.return_value = [
            self._make_mock_result([
                {"cls": 0, "conf": 0.95, "xyxy": [5.0, 10.0, 50.0, 80.0]},
            ])
        ]
        mock_get_model.return_value = mock_model

        _mock_deps.get_yolo_world_conf = lambda: 0.15
        _mock_registry.get_category = lambda name: None

        results = detect("/fake/person.jpg")
        assert len(results) == 1
        assert results[0]["label"] == "person"
        assert results[0]["category"] is None

    @patch("app.services.detection._get_model")
    def test_detect_passes_conf_and_iou(self, mock_get_model):
        mock_model = MagicMock()
        result = MagicMock()
        result.boxes = None
        mock_model.predict.return_value = [result]
        mock_get_model.return_value = mock_model

        _mock_deps.get_yolo_world_conf = lambda: 0.15

        detect("/fake/photo.jpg")

        call_kwargs = mock_model.predict.call_args[1]
        assert call_kwargs["conf"] == 0.15
        assert call_kwargs["iou"] == 0.45
        assert call_kwargs["verbose"] is False
