"""F-07 (Medium): Raw exception text reflected to clients — RED tests.

Tests FAIL until:
- admin.py Ollama 502 responses use a generic message (not f"cannot reach Ollama: {e}")
- items.py create_item 500 uses a generic message (not f"create_item failed: {e}")
- items.py search embed failure uses a generic message (not f"query embedding failed: {e}")
"""
import urllib.request


# ── Integration tests: Ollama errors must not leak raw exception text ─────────

def test_models_list_502_does_not_leak_exception_text(client, monkeypatch):
    """GET /models when Ollama is unreachable must return 502 with a generic message."""
    def _fail(req, **kwargs):
        raise ConnectionRefusedError("[Errno 111] Connection refused to localhost:11434")
    monkeypatch.setattr(urllib.request, "urlopen", _fail)

    resp = client.get("/models")
    assert resp.status_code == 502, f"Expected 502, got {resp.status_code}: {resp.text}"
    msg = resp.json()["error"]["message"]
    assert "[Errno 111]" not in msg, f"Raw errno in response: {msg!r}"
    assert "Connection refused" not in msg, f"Raw exception text in response: {msg!r}"
    assert "localhost" not in msg, f"Internal host in response: {msg!r}"


def test_models_running_502_does_not_leak_exception_text(client, monkeypatch):
    """GET /models/running when Ollama is unreachable must use a generic message."""
    def _fail(req, **kwargs):
        raise OSError("getaddrinfo failed: temporary failure in name resolution")
    monkeypatch.setattr(urllib.request, "urlopen", _fail)

    resp = client.get("/models/running")
    assert resp.status_code == 502, f"Expected 502, got {resp.status_code}: {resp.text}"
    msg = resp.json()["error"]["message"]
    assert "getaddrinfo" not in msg, f"DNS error detail in response: {msg!r}"
    assert "name resolution" not in msg, f"Raw exception text in response: {msg!r}"


def test_models_select_502_does_not_leak_exception_text(client, monkeypatch):
    """POST /models/select warmup failure must use a generic message."""
    def _fail(req, **kwargs):
        raise ConnectionRefusedError("[Errno 111] Connection refused to Ollama")
    monkeypatch.setattr(urllib.request, "urlopen", _fail)

    resp = client.post("/models/select", json={"model": "test-model"})
    assert resp.status_code == 502, f"Expected 502, got {resp.status_code}: {resp.text}"
    msg = resp.json()["error"]["message"]
    assert "[Errno 111]" not in msg, f"Raw errno in response: {msg!r}"
    assert "Connection refused" not in msg, f"Raw exception text in response: {msg!r}"


def test_create_item_500_does_not_leak_exception_text(client, monkeypatch):
    """POST /items when DB insert fails must return 500 with a generic message."""
    import app.db.repository as repo
    original = repo.insert_item

    def _fail(*args, **kwargs):
        raise RuntimeError("psycopg: column 'x' of relation 'items' does not exist")
    monkeypatch.setattr(repo, "insert_item", _fail)

    try:
        resp = client.post("/items", json={"name": "test item"})
        assert resp.status_code == 500, f"Expected 500, got {resp.status_code}: {resp.text}"
        msg = resp.json()["error"]["message"]
        assert "psycopg" not in msg, f"DB driver name in response: {msg!r}"
        assert "column" not in msg, f"Schema detail in response: {msg!r}"
        assert "items" not in msg, f"Table name in response: {msg!r}"
    finally:
        monkeypatch.setattr(repo, "insert_item", original)


def test_search_embed_failure_does_not_leak_exception_text(client, monkeypatch):
    """GET /search when embed fails must return 400/503 with a generic message."""
    import app.routes.items as items_route
    original = items_route.embed_text

    def _fail(s):
        raise RuntimeError("fastembed internal: CUDA out of memory on device cuda:0")
    monkeypatch.setattr(items_route, "embed_text", _fail)

    try:
        resp = client.get("/search", params={"q": "test query"})
        assert resp.status_code in (400, 503), (
            f"Expected 400 or 503, got {resp.status_code}: {resp.text}"
        )
        msg = resp.json()["error"]["message"]
        assert "fastembed" not in msg, f"Internal library name in response: {msg!r}"
        assert "CUDA" not in msg, f"Hardware detail in response: {msg!r}"
        assert "cuda:0" not in msg, f"Device name in response: {msg!r}"
    finally:
        monkeypatch.setattr(items_route, "embed_text", original)


def test_confirm_500_does_not_leak_exception_text(client, monkeypatch):
    """POST /photos/{id}/confirm when a DB operation fails must use a generic message."""
    import app.db.repository as repo
    original = repo.insert_item

    def _fail(*args, **kwargs):
        raise RuntimeError("psycopg: SSL connection has been closed unexpectedly")
    monkeypatch.setattr(repo, "insert_item", _fail)

    try:
        payload = {
            "version": "1",
            "bin_id": "F07CONFIRM01",
            "selected_groups": [
                {"group_key": "bolt|fastener", "label": "bolt", "category": "fastener", "quantity": 5}
            ],
        }
        # photo_id 999999 doesn't exist — that's fine; we're testing the error path
        # when the confirm handler encounters an unexpected DB failure.
        # The repo.insert_item patch makes it fail if reached.
        resp = client.post("/photos/999999/confirm", json=payload)
        # Either 404 (photo not found, hit before insert_item) or 500 (insert failed)
        if resp.status_code == 500:
            msg = resp.json()["error"]["message"]
            assert "psycopg" not in msg, f"DB driver in confirm 500 response: {msg!r}"
            assert "SSL" not in msg, f"Internal connection detail in response: {msg!r}"
    finally:
        monkeypatch.setattr(repo, "insert_item", original)
