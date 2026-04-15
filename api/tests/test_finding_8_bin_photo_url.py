"""Finding #8: GET /bins/{id} photo records must expose `url` (not `path`).

iOS `PhotoRecord` decoder requires a URL pointing at the authenticated
/photos/{photo_id}/file endpoint. F-10 still forbids `path`.
"""


def test_get_bin_photos_include_url(client, valid_jpeg_bytes):
    """Each photo entry on GET /bins/{id} must include url = /photos/{id}/file."""
    client.post(
        "/ingest",
        data={"bin_id": "F8URL01"},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    resp = client.get("/bins/F8URL01")
    assert resp.status_code == 200, resp.text
    photos = resp.json()["photos"]
    assert photos, "expected at least one photo"
    for p in photos:
        assert "path" not in p, f"path must not be exposed: {p}"
        assert p.get("url") == f"/photos/{p['photo_id']}/file", p


def test_openapi_get_bin_response_shape_has_url_not_path(client):
    """OpenAPI schema for GET /bins/{bin_id} must declare `url` on PhotoRecord,
    not `path`."""
    schema = client.get("/openapi.json").json()
    get_bin = schema["paths"]["/bins/{bin_id}"]["get"]
    # A real response_model must produce a non-empty 200 response schema.
    content = get_bin["responses"]["200"]["content"]["application/json"]
    ref = content["schema"].get("$ref") or content["schema"]
    assert ref, "GET /bins/{bin_id} response schema must not be empty"

    # Walk $refs until we find the photos item shape.
    def resolve(node):
        if isinstance(node, dict) and "$ref" in node:
            name = node["$ref"].split("/")[-1]
            return schema["components"]["schemas"][name]
        return node

    resp_schema = resolve(content["schema"])
    photos_prop = resp_schema["properties"]["photos"]
    photo_item = resolve(photos_prop["items"])
    props = photo_item.get("properties", {})
    assert "url" in props, f"photo item must declare url: {props}"
    assert "path" not in props, f"photo item must not declare path: {props}"


def test_ingest_and_add_photos_include_url(client, valid_jpeg_bytes):
    """/ingest and /bins/{id}/add photo records expose url not path (consistent
    with PhotoRecord contract)."""
    ing = client.post(
        "/ingest",
        data={"bin_id": "F8URL02"},
        files=[("photos", ("a.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert ing.status_code == 200
    for p in ing.json()["photos"]:
        assert "path" not in p
        assert p.get("url") == f"/photos/{p['photo_id']}/file"

    add = client.post(
        "/bins/F8URL02/add",
        files=[("photos", ("b.jpg", valid_jpeg_bytes, "image/jpeg"))],
    )
    assert add.status_code == 200, add.text
    for p in add.json()["photos"]:
        assert "path" not in p
        assert p.get("url") == f"/photos/{p['photo_id']}/file"
