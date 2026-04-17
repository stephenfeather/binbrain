"""Unit tests for the in-process /suggest progress registry.

Design note: thoughts/shared/designs/suggest-heartbeat-2026-04-17.md (iOS repo).
"""
import pytest


@pytest.fixture()
def tracker():
    # Fresh tracker instance per test — don't pollute the module-level singleton.
    from app.services.suggest_tracker import SuggestTracker
    return SuggestTracker()


def test_start_creates_running_vision_entry(tracker):
    tracker.start(42)
    entry = tracker.get(42)
    assert entry is not None
    assert entry.state == "running"
    assert entry.stage == "vision"
    assert entry.error_code is None


def test_start_elapsed_ms_is_monotonic(tracker, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        "app.services.suggest_tracker.monotonic", lambda: clock[0]
    )
    tracker.start(1)
    clock[0] = 100.5
    elapsed_a = tracker.elapsed_ms(tracker.get(1))
    clock[0] = 101.25
    elapsed_b = tracker.elapsed_ms(tracker.get(1))
    assert elapsed_a == 500
    assert elapsed_b == 1250


def test_update_stage_transitions_within_running(tracker):
    tracker.start(1)
    tracker.update_stage(1, "embedding_match")
    assert tracker.get(1).stage == "embedding_match"


def test_update_stage_is_noop_after_terminal(tracker):
    tracker.start(1)
    tracker.mark_done(1)
    tracker.update_stage(1, "embedding_match")  # should not change terminal entry
    entry = tracker.get(1)
    assert entry.state == "done"
    assert entry.stage == "final"


def test_mark_done_freezes_elapsed(tracker, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(
        "app.services.suggest_tracker.monotonic", lambda: clock[0]
    )
    tracker.start(1)
    clock[0] = 12.0
    tracker.mark_done(1)
    # Advance clock 60s more — elapsed should not grow past the terminal mark.
    clock[0] = 72.0
    entry = tracker.get(1)
    assert entry.state == "done"
    assert entry.stage == "final"
    assert tracker.elapsed_ms(entry) == 2000


def test_mark_failed_sets_error_code(tracker):
    tracker.start(1)
    tracker.mark_failed(1, "timeout")
    entry = tracker.get(1)
    assert entry.state == "failed"
    assert entry.error_code == "timeout"


def test_get_unknown_photo_returns_none(tracker):
    assert tracker.get(9999) is None


def test_terminal_entries_pruned_after_ttl(tracker, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        "app.services.suggest_tracker.monotonic", lambda: clock[0]
    )
    tracker.start(1)
    clock[0] = 1.0
    tracker.mark_done(1)
    # Just under TTL — still present.
    clock[0] = 1.0 + 299
    assert tracker.get(1) is not None
    # Past TTL — pruned on next access.
    clock[0] = 1.0 + 301
    assert tracker.get(1) is None


def test_running_entries_never_pruned_by_ttl(tracker, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        "app.services.suggest_tracker.monotonic", lambda: clock[0]
    )
    tracker.start(1)
    # Huge gap while still running — should not be pruned.
    clock[0] = 10_000.0
    entry = tracker.get(1)
    assert entry is not None
    assert entry.state == "running"


def test_restart_overwrites_previous_entry(tracker):
    tracker.start(1)
    tracker.mark_failed(1, "transient")
    tracker.start(1)  # a second /suggest call for the same photo
    entry = tracker.get(1)
    assert entry.state == "running"
    assert entry.stage == "vision"
    assert entry.error_code is None
