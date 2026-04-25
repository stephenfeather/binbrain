import pytest
from app.db.repository import _normalize_category
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


def test_project_upcitemdb_response_happy_path():
    # ApiDev2_002b (SEC-24-2): allow-list projection strips non-allowed keys
    # from items[0] and preserves top-level code/total. Ancillary fields
    # (images, description, offers, etc.) must not survive.
    from app.services.upc_lookup import _project_upcitemdb_response

    body = {
        "code": "OK",
        "total": 1,
        "extra_top_level": "drop me",
        "items": [
            {
                "title": "Allow-list Test",
                "category": "Hardware",
                "brand": "Bench",
                "upc": "012345678905",
                "ean": "0012345678905",
                "description": "should be dropped",
                "images": ["https://example.com/a.jpg"],
                "offers": [{"merchant": "x", "price": 9.99}],
            }
        ],
    }

    projected = _project_upcitemdb_response(body)

    assert projected == {
        "code": "OK",
        "total": 1,
        "items": [
            {
                "title": "Allow-list Test",
                "category": "Hardware",
                "brand": "Bench",
                "upc": "012345678905",
                "ean": "0012345678905",
            }
        ],
    }
    assert "extra_top_level" not in projected
    assert "description" not in projected["items"][0]
    assert "images" not in projected["items"][0]
    assert "offers" not in projected["items"][0]


def test_project_upcitemdb_response_missing_items():
    # Empty / absent items[] is a valid upstream response (code=OK,total=0).
    # Projection must yield items=[] rather than items=[{}].
    from app.services.upc_lookup import _project_upcitemdb_response

    assert _project_upcitemdb_response({"code": "OK", "total": 0}) == {
        "code": "OK",
        "total": 0,
        "items": [],
    }
    assert _project_upcitemdb_response({"code": "OK", "total": 0, "items": []}) == {
        "code": "OK",
        "total": 0,
        "items": [],
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("electronics", "electronics"),
        ("Electronics", "electronics"),
        ("  Electronics  ", "electronics"),
        ("MixedCase", "mixedcase"),
        ("\tFastener\n", "fastener"),
    ],
)
def test_normalize_category(raw, expected):
    assert _normalize_category(raw) == expected


class TestExtractUpcFromDeviceMetadata:
    def _picker(self):
        from app.services.upc_lookup import extract_upc_from_device_metadata

        return extract_upc_from_device_metadata

    def test_none_metadata(self):
        assert self._picker()(None) is None

    def test_non_dict_metadata(self):
        assert self._picker()("not a dict") is None

    def test_missing_device_processing(self):
        assert self._picker()({}) is None

    def test_device_processing_not_dict(self):
        assert self._picker()({"device_processing": "nope"}) is None

    def test_missing_barcodes(self):
        assert self._picker()({"device_processing": {}}) is None

    def test_barcodes_not_list(self):
        assert self._picker()({"device_processing": {"barcodes": "x"}}) is None

    def test_empty_barcodes(self):
        assert self._picker()({"device_processing": {"barcodes": []}}) is None

    def test_picks_valid_upca(self):
        meta = {
            "device_processing": {"barcodes": [{"payload": "012345678905", "symbology": "UPC-A"}]}
        }
        assert self._picker()(meta) == "012345678905"

    def test_picks_valid_ean13(self):
        meta = {
            "device_processing": {"barcodes": [{"payload": "4005176834561", "symbology": "EAN-13"}]}
        }
        assert self._picker()(meta) == "4005176834561"

    def test_skips_invalid_picks_first_valid(self):
        meta = {
            "device_processing": {
                "barcodes": [
                    {"payload": "abc", "symbology": "QR"},
                    {"payload": "12345"},
                    {"payload": "012345678905", "symbology": "UPC-A"},
                    {"payload": "4005176834561", "symbology": "EAN-13"},
                ]
            }
        }
        assert self._picker()(meta) == "012345678905"

    def test_skips_non_dict_entries(self):
        meta = {"device_processing": {"barcodes": ["string", None, {"payload": "012345678905"}]}}
        assert self._picker()(meta) == "012345678905"

    def test_non_string_payload(self):
        meta = {"device_processing": {"barcodes": [{"payload": 12345}]}}
        assert self._picker()(meta) is None

    def test_no_valid_upcs(self):
        meta = {
            "device_processing": {
                "barcodes": [
                    {"payload": "abc"},
                    {"payload": "12345"},
                    {"payload": "abcdefghijkl"},
                ]
            }
        }
        assert self._picker()(meta) is None


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
