import pytest


def fingerprint(name: str, category: str | None) -> str:
    name_part = (name or "").strip().lower()
    cat_part = (category or "").strip().lower()
    return f"{name_part}|{cat_part}"


def test_fingerprint_generation():
    assert fingerprint(" M3 Bolt ", "Fastener") == "m3 bolt|fastener"
    assert fingerprint("Widget", None) == "widget|"
    assert fingerprint("  Mixed  Case ", "  ") == "mixed  case|"


def test_embedding_dimension_validation(client, app_module, monkeypatch):
    def bad_embed(_):
        return [0.0] * 10

    monkeypatch.setattr(app_module, "embed_text", bad_embed)
    resp = client.post(
        "/items",
        data={"name": "test item", "category": "test", "notes": "n"},
    )
    assert resp.status_code == 500
    assert "unexpected embedding dims" in resp.text
