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


def test_lookup_upcitemdb_populates_raw_response_and_elapsed_ms(monkeypatch):
    # ApiDev2_002 (Gap #7): _lookup_upcitemdb must carry the full upstream
    # body and the network latency up to the route so item_upc_lookups can
    # persist them as provenance. Monkeypatch urlopen to a canned payload
    # and assert both fields arrive non-None on the success path.
    import io
    import json as _json

    import app.services.upc_lookup as upc_mod
    from app.services.upc_lookup import _lookup_upcitemdb

    canned = {
        "code": "OK",
        "total": 1,
        "items": [
            {
                "title": "Unit Prov Product",
                "category": "Hardware > Tools > Wrenches",
                "brand": "Bench",
            }
        ],
    }

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(_req, timeout):
        return _Resp(_json.dumps(canned).encode())

    monkeypatch.setattr(upc_mod.urllib.request, "urlopen", fake_urlopen)

    result = _lookup_upcitemdb("012345678905")

    assert result is not None
    assert result.name == "Unit Prov Product"
    assert result.category == "Hardware"
    assert result.source == "upcitemdb"
    assert result.raw_response == canned
    assert result.elapsed_ms is not None
    assert result.elapsed_ms >= 0


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
