# Bin Brain — iOS Client: Server Changes Required

> Generated: 2026-02-25
> iOS spec: `binbrain-ios/SPEC.md`
> Backend spec: `openapi.yaml`

This document tracks every gap between what the iOS app needs and what the backend currently provides. Items are split by urgency: **v1 blockers** (must be resolved before the app ships), **post-v1 enhancements** (deferred by deliberate v1 decision), and **clarifications** (ambiguities that affect iOS implementation correctness).

---

## 1. Clarifications Needed Now

These ambiguities block correct iOS client implementation.

### 1.1 Search Distance Field: Raw pgvector (0–2) or Pre-Normalized (0–1)?

**Impact:** Determines the score conversion formula used everywhere in the iOS app.

The OpenAPI spec and the hardened iOS spec contradict each other:

| Source | Formula | Assumption |
|--------|---------|------------|
| `openapi.yaml` (description + min_score example) | `score ≈ 1 − distance` | Distance returned in **0–1** range |
| `SPEC.md §8 + §15.5` | `score = 1.0 − (distance / 2.0)` | Distance returned as raw pgvector (0–2) |

The OpenAPI example values (`distance: 0.04`, `distance: 0.31`) and the comment "`min_score = 0.6` ≈ `distance ≤ 0.4`" are consistent with the 0–1 range. The iOS spec hardening identified that raw pgvector cosine distance is 0–2 (where `score = 1 − distance` produces negative values).

**Question for backend:** Does `/search` return the raw pgvector `<=>` distance value, or has it already been divided by 2 (or converted to similarity) before serialization?

**iOS will use:** `score = 1.0 − (distance / 2.0)` (SPEC.md decision) until the backend confirms otherwise.

---

## 2. Post-v1 Enhancements (Deferred by Design)

These are features the iOS app intentionally omits in v1 due to missing backend support. Each has a v1 workaround documented in the iOS spec.

### 2.1 `GET /photos/{photo_id}/file` — Photo File Serving

**Needed for:** Photo gallery in Bin Detail (swipe-up sheet with thumbnails + full-size view).

**v1 workaround:** Photo gallery cut entirely from v1. Bin detail header shows a photo count badge only ("3 photos"). No images are displayed.

**Backend work:** Add an endpoint that streams the stored image file by `photo_id`. The `PhotoRecord.path` field contains the container-internal path (e.g., `/data/photos/B-42/abc123.jpg`); the endpoint just needs to serve that file.

**Suggested spec addition:**
```yaml
/photos/{photo_id}/file:
  get:
    summary: Serve a stored photo file
    operationId: getPhotoFile
    parameters:
      - $ref: "#/components/parameters/PhotoIdPath"
    responses:
      "200":
        description: Photo file content.
        content:
          image/jpeg: {}
          image/heic: {}
          image/png: {}
          image/webp: {}
      "404":
        $ref: "#/components/responses/NotFound"
```

---

### 2.2 `DELETE /bins/{bin_id}/items/{item_id}` — Remove Item from Bin

**Needed for:** Swipe-to-delete a bin-item association in Bin Detail (removes the association, not the item from the catalogue).

**v1 workaround:** Swipe-to-delete omitted. Items in a bin are add-only in v1.

**Backend work:** Add an endpoint to delete the row in the `bin_items` join table for a specific `(bin_id, item_id)` pair. Should not delete the item from the global catalogue — only removes the association.

**Suggested spec addition:**
```yaml
/bins/{bin_id}/items/{item_id}:
  delete:
    summary: Remove an item from a bin
    operationId: removeBinItem
    parameters:
      - $ref: "#/components/parameters/BinIdPath"
      - name: item_id
        in: path
        required: true
        schema: { type: integer, minimum: 1 }
    responses:
      "204":
        description: Association removed.
      "404":
        $ref: "#/components/responses/NotFound"
```

---

### 2.3 `PATCH /bins/{bin_id}/items/{item_id}` — Update Item Quantity in Bin

**Needed for:** Inline quantity editing in Bin Detail.

**v1 workaround:** Quantity shown read-only. Tap-to-edit not implemented.

**Backend work:** Add an endpoint to update the `quantity` (and optionally `confidence`) for a specific bin-item association. The `BinItemRecord` in `GET /bins/{bin_id}` already returns `quantity` per bin, so storage is presumably in the `bin_items` join table.

**Suggested spec addition:**
```yaml
/bins/{bin_id}/items/{item_id}:
  patch:
    summary: Update a bin-item association
    operationId: updateBinItem
    parameters:
      - $ref: "#/components/parameters/BinIdPath"
      - name: item_id
        in: path
        required: true
        schema: { type: integer, minimum: 1 }
    requestBody:
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              quantity: { type: number, minimum: 0 }
              confidence: { type: number, minimum: 0, maximum: 1 }
    responses:
      "200":
        description: Association updated.
      "404":
        $ref: "#/components/responses/NotFound"
```

---

### 2.4 `DELETE /photos/{photo_id}` — Delete a Photo

**Needed for:** Photo deletion from the gallery.

**v1 status:** Moot — the gallery is cut from v1. This endpoint is low priority until the gallery is implemented.

**Backend work:** Soft-delete or hard-delete a `PhotoRecord`. Should cascade or remove the association from `bin_photos`. Filesystem cleanup (deleting the stored file) is a backend concern.

---

### 2.5 Real Detection Model for `/photos/{photo_id}/detect`

**Needed for:** The detect→groups→confirm pipeline (v2 AI workflow).

**v1 status:** The iOS app does not call `/detect` or `/confirm` at all. The current stub model makes this pipeline non-functional.

**Backend work:** Replace the stub detection model with a real one (Hailo-accelerated YOLO or similar). Once this works, the iOS app can implement the bounding-box review UI in v2.

---

## 3. OpenAPI Spec Inconsistencies (Housekeeping)

Minor issues in the current `openapi.yaml` that don't block v1 but should be cleaned up.

### 3.1 `SearchResponse` Missing `version` Envelope Field

Every other success response in the spec includes `"version": "1"` at the top level (per the envelope convention documented at the top of `openapi.yaml`). `SearchResponse` does not:

```yaml
# Current SearchResponse — missing version
SearchResponse:
  type: object
  required: [q, limit, offset, results]
  properties:
    q:      { type: string }
    limit:  { type: integer }
    offset: { type: integer }
    ...
```

The iOS app will not break if `version` is absent (it's not decoded for search responses), but it's inconsistent with the rest of the API surface.

**Fix:** Add `version: { type: string, enum: ["1"] }` to `SearchResponse.properties` and include it in `required`.

### 3.2 `SearchResultItem.bins` Not Marked as Required

The `bins` array (list of bin IDs where the matched item appears) is not in the `required` list of `SearchResultItem`:

```yaml
SearchResultItem:
  required: [item_id, name, distance]   # bins missing here
  properties:
    ...
    bins:
      type: array
      items: { type: string }
```

The iOS app displays "Found in BIN-0001, BIN-0042" from this field. If it can be absent, the iOS app needs to handle `nil`/missing. If it's always returned, mark it required.

**Fix:** Either add `bins` to `required` (if always present, even as `[]`) or document when it can be absent.

---

### 3.3 Distance vs Score Inconsistency Between `/search` and `/suggest`

The two main data-retrieval endpoints return similarity information in different forms:

| Endpoint | Field | Value | Formula to get 0–1 score |
|----------|-------|-------|--------------------------|
| `GET /search` | `distance` | Raw pgvector `<=>` cosine distance (0–2) | `score = 1.0 - (distance / 2.0)` |
| `GET /photos/{id}/suggest` | `confidence` | Already computed as `1.0 - distance` (0–1) | None — already a score |

Verified from backend source: `api/app/db/repository.py` uses `(e.embedding <=> CAST(:qvec AS vector)) AS distance` with no transformation for `/search`, while the suggest function returns `1.0 - distance` aliased as `score`.

**Fix:** Either normalize both endpoints to return raw distance (consistent, client converts) or return a 0–1 score from both (simpler for clients). Document the chosen convention in the OpenAPI spec. The iOS client handles the current asymmetry correctly but it is a footgun for future API consumers.

---

## 4. Summary Table

| # | Item | Priority | iOS v1 Impact |
|---|------|----------|---------------|
| 1.1 | Distance field range clarification | **Now** | Affects score formula throughout app |
| 2.1 | `GET /photos/{id}/file` | Post-v1 | Unblocks photo gallery feature |
| 2.2 | `DELETE /bins/{id}/items/{id}` | Post-v1 | Unblocks swipe-to-delete |
| 2.3 | `PATCH /bins/{id}/items/{id}` | Post-v1 | Unblocks inline quantity edit |
| 2.4 | `DELETE /photos/{id}` | Post-v1 | Unblocks photo deletion |
| 2.5 | Real detection model | Post-v1 / v2 | Unblocks bounding-box review UI |
| 3.1 | `SearchResponse` version field | Housekeeping | No iOS impact |
| 3.2 | `SearchResultItem.bins` required | Housekeeping | Minor defensive coding impact |
