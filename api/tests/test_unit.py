import pytest

from app.services.upc_lookup import _simplify_category, validate_upc


def fingerprint(name: str, category: str | None) -> str:
    name_part = (name or "").strip().lower()
    cat_part = (category or "").strip().lower()
    return f"{name_part}|{cat_part}"


def test_fingerprint_generation():
    assert fingerprint(" M3 Bolt ", "Fastener") == "m3 bolt|fastener"
    assert fingerprint("Widget", None) == "widget|"
    assert fingerprint("  Mixed  Case ", "  ") == "mixed  case|"


def test_validate_upc_12_digits():
    assert validate_upc("012345678901") is True


def test_validate_upc_13_digits():
    assert validate_upc("0012345678905") is True


def test_validate_upc_too_short():
    assert validate_upc("12345") is False


def test_validate_upc_too_long():
    assert validate_upc("12345678901234") is False


def test_validate_upc_non_digits():
    assert validate_upc("abcdefghijkl") is False


def test_validate_upc_empty():
    assert validate_upc("") is False


def test_simplify_category_nested():
    assert _simplify_category("Electronics > Computers > Laptops") == "Electronics"


def test_simplify_category_single():
    assert _simplify_category("Health & Beauty") == "Health & Beauty"


def test_simplify_category_none():
    assert _simplify_category(None) is None


def test_simplify_category_empty():
    assert _simplify_category("") is None


def test_embedding_dimension_validation(client, app_module, monkeypatch):
    import app.routes.items as items_mod

    def bad_embed(_):
        return [0.0] * 10

    monkeypatch.setattr(items_mod, "embed_text", bad_embed)
    resp = client.post(
        "/items",
        json={"name": "test item", "category": "test", "notes": "n"},
    )
    assert resp.status_code == 500
    assert "unexpected embedding dims" in resp.text
