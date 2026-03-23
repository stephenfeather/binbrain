"""Unit tests for the class registry service."""
from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock, patch

# Ensure the real class_registry is loaded (not a stub from other test files)
if "app.services.class_registry" in sys.modules:
    _mod = sys.modules["app.services.class_registry"]
    if not hasattr(_mod, "_classes"):
        del sys.modules["app.services.class_registry"]

from app.services import class_registry


class TestClassRegistry:
    def setup_method(self):
        """Reset module state before each test."""
        class_registry._classes.clear()
        class_registry._categories.clear()
        class_registry._on_reload = None

    def test_load_from_db(self):
        mock_db = MagicMock()
        with patch(
            "app.db.repository.fetch_active_classes",
            return_value=[
                {"class_name": "scissors", "category": "tools"},
                {"class_name": "bottle", "category": "household"},
            ],
        ):
            class_registry.load_from_db(mock_db)

        assert class_registry.get_classes() == ["scissors", "bottle"]
        assert class_registry.get_category("scissors") == "tools"
        assert class_registry.get_category("bottle") == "household"

    def test_get_classes_returns_copy(self):
        class_registry._classes.extend(["a", "b"])
        result = class_registry.get_classes()
        result.append("c")
        assert class_registry.get_classes() == ["a", "b"]

    def test_get_category_unknown(self):
        assert class_registry.get_category("nonexistent") is None

    def test_add_class_new(self):
        mock_db = MagicMock()
        with patch(
            "app.db.repository.insert_confirmed_class",
            return_value={"id": 1, "class_name": "wrench"},
        ):
            result = class_registry.add_class(mock_db, "Wrench", "tools", "manual")

        assert result is True
        assert "wrench" in class_registry.get_classes()
        assert class_registry.get_category("wrench") == "tools"
        mock_db.commit.assert_called_once()

    def test_add_class_duplicate_in_memory(self):
        class_registry._classes.append("wrench")
        class_registry._categories["wrench"] = "tools"
        mock_db = MagicMock()
        result = class_registry.add_class(mock_db, "Wrench", "tools", "manual")

        assert result is False
        mock_db.commit.assert_not_called()

    def test_add_class_duplicate_in_db(self):
        mock_db = MagicMock()
        with patch(
            "app.db.repository.insert_confirmed_class",
            return_value=None,
        ):
            result = class_registry.add_class(mock_db, "wrench", "tools", "manual")

        assert result is False

    def test_remove_class(self):
        class_registry._classes.append("wrench")
        class_registry._categories["wrench"] = "tools"
        mock_db = MagicMock()
        with patch("app.db.repository.soft_delete_class", return_value=True):
            result = class_registry.remove_class(mock_db, "wrench")

        assert result is True
        assert "wrench" not in class_registry.get_classes()
        assert class_registry.get_category("wrench") is None
        mock_db.commit.assert_called_once()

    def test_remove_class_not_found(self):
        mock_db = MagicMock()
        with patch("app.db.repository.soft_delete_class", return_value=False):
            result = class_registry.remove_class(mock_db, "nonexistent")

        assert result is False

    def test_reload_callback_fires_on_add(self):
        callback = MagicMock()
        class_registry.set_reload_callback(callback)
        mock_db = MagicMock()

        with patch(
            "app.db.repository.insert_confirmed_class",
            return_value={"id": 1, "class_name": "bolt"},
        ):
            class_registry.add_class(mock_db, "bolt", "fastener", "vision_llm")

        time.sleep(0.2)
        callback.assert_called_once()
        assert "bolt" in callback.call_args[0][0]

    def test_reload_callback_fires_on_remove(self):
        class_registry._classes.append("bolt")
        class_registry._categories["bolt"] = "fastener"
        callback = MagicMock()
        class_registry.set_reload_callback(callback)
        mock_db = MagicMock()

        with patch("app.db.repository.soft_delete_class", return_value=True):
            class_registry.remove_class(mock_db, "bolt")

        time.sleep(0.2)
        callback.assert_called_once()
        assert "bolt" not in callback.call_args[0][0]

    def test_class_count(self):
        class_registry._classes.extend(["a", "b", "c"])
        assert class_registry.class_count() == 3

    def test_no_reload_callback_when_not_set(self):
        """Ensure no error when reload callback is None."""
        mock_db = MagicMock()
        with patch(
            "app.db.repository.insert_confirmed_class",
            return_value={"id": 1, "class_name": "test"},
        ):
            result = class_registry.add_class(mock_db, "test", None, "manual")
        assert result is True
