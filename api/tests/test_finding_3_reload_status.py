"""Finding #3: surface YOLOE reload failures so silent crashes stop hiding.

The live container failed on `os.mkdir('/models')` with PermissionError; HTTP
200 had already returned, so new classes silently never loaded. These tests
pin down:
  * the reload wrapper captures errors (never raises),
  * the status is retrievable and reported on /health,
  * the config default points at a writable directory.
"""

from __future__ import annotations


def test_models_dir_default_is_writable_path():
    """Default MODELS_DIR must not be the un-writable container root '/models'."""
    from app.config import MODELS_DIR

    assert str(MODELS_DIR) != "/models", (
        "Default MODELS_DIR must not be '/models' (root dir is un-writable for "
        "the app user; see Finding #3)."
    )


def test_reload_status_reports_success(monkeypatch):
    """Calling detection.reload_classes should record a success status."""
    from app.services import detection

    class _StubModel:
        def set_classes(self, classes):
            self.last = classes

    monkeypatch.setattr(detection, "_get_model", lambda: _StubModel())

    detection.reload_classes(["pencil", "screw"])
    status = detection.get_reload_status()

    assert status["status"] == "ok", status
    assert status["count"] == 2
    assert status["error"] is None
    assert status["last_reload_at"] is not None


def test_reload_status_reports_failure_without_raising(monkeypatch):
    """If anything inside reload explodes, the wrapper must capture the error
    (not propagate) and record a failure status.  This is the exact contract
    that was missing when Finding #3 hit production."""
    from app.services import detection

    def _boom():
        raise PermissionError("[Errno 13] mkdir '/models'")

    monkeypatch.setattr(detection, "_get_model", _boom)

    # Must not raise — matches the background-thread contract.
    detection.reload_classes(["pencil"])

    status = detection.get_reload_status()
    assert status["status"] == "error", status
    assert "mkdir" in status["error"]
    assert status["count"] == 1


def test_health_reports_model_reload_status(client, monkeypatch):
    """/health surfaces the last reload status so ops can see silent failures."""
    from app.services import detection

    class _StubModel:
        def set_classes(self, classes):
            return None

    monkeypatch.setattr(detection, "_get_model", lambda: _StubModel())
    detection.reload_classes(["hammer"])

    body = client.get("/health").json()
    reload_info = body.get("model_reload")
    assert reload_info is not None, f"/health must include model_reload: {body}"
    assert reload_info["status"] == "ok"
    assert reload_info["count"] == 1
    assert reload_info["last_reload_at"] is not None
