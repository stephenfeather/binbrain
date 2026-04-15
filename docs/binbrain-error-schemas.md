##Recommended error.code values (small stable set)

- Keep this list short so Shortcuts can branch on it:
- bad_request
- not_found
- conflict
- payload_too_large
- unsupported_media_type
- rate_limited
- internal_error
- service_unavailable

##Recommended HTTP Status Codes Per Endpoint

###GET /health

- 200 OK — always when app is up
- 503 Service Unavailable — if you choose to include DB connectivity checks and DB is down

---

###POST /ingest

Success

- 200 OK — recommended if you always return a JSON response body
- 201 Created — also acceptable, but 200 is simpler for Shortcuts

Client errors

- 400 Bad Request
	- missing/blank bin_id
	- no photos supplied

- 413 Payload Too Large
	- upload exceeds configured limit

- 415 Unsupported Media Type
	- if you want to reject unknown file types (optional; otherwise accept and store)

Server errors

- 500 Internal Server Error
	- filesystem write failed, DB insert failed

- 503 Service Unavailable
	- DB unreachable

---

###GET /photos/{photo_id}/suggest

Success

- 200 OK

Client errors

- 404 Not Found
	- photo_id doesn’t exist

409 Conflict

photo exists but file path missing or unreadable (optional; otherwise use 500)

Server errors

- 500 Internal Server Error
	- unexpected exceptions

- 503 Service Unavailable
	- inference subsystem down (future)

---

###POST /photos/{photo_id}/detect

(If you implement detect as POST; same rules for GET if you go that route.)

Success

- 200 OK

Client errors

- 404 Not Found
	- photo_id not found

- 409 Conflict
	- photo exists but image file missing/unreadable OR detection model not configured (I like 503 for model-not-ready; see below)

Server errors

- 500 Internal Server Error
	- crash in pipeline

- 503 Service Unavailable
	- model runtime not installed / not ready (e.g., Hailo service down)

---

###GET /photos/{photo_id}/groups

Success

- 200 OK
	- return cached groups if present
	- or compute from raw detections

Client errors

- 404 Not Found
	- photo_id not found

- 409 Conflict
	- no detections exist yet and you don’t want to auto-run detect
	- (Alternative: auto-run detect; then 200)

Server errors

- 500 Internal Server Error

- 503 Service Unavailable
	- if it tries to run detect and model runtime is unavailable

---

POST /photos/{photo_id}/confirm

Success

- 200 OK
	- idempotent confirm is easiest: repeated calls return 200 with results
	- (Avoid 201 here; it may both create and link.)

Client errors

- 400 Bad Request
	- invalid body schema
	- empty selected_groups
	- missing bin_id

- 404 Not Found
	- photo_id not found
	- bin_id not found (optional; I’d auto-create bins, so usually not needed)

- 409 Conflict
	- selected_groups contains unknown group_key for that photo (good use of 409)
	- data integrity conflict (rare)

Server errors

- 500 Internal Server Error
- 503 Service Unavailable
	- DB or embedding service down

---

###POST /items

Success

- 200 OK
	- Since it’s upsert, 200 is the cleanest

Client errors

- 400 Bad Request
	- missing name
	- invalid types

- 409 Conflict (optional)
	- if you ever enforce category constraints and it conflicts (not needed now)

Server errors

- 500 Internal Server Error
	- embedding failure, DB failure

- 503 Service Unavailable
	- DB down

---

###GET /search

Success

- 200 OK

Client errors

- 400 Bad Request
	- missing/empty q
	- invalid limit/offset

- 422 Unprocessable Entity
	- FastAPI may emit 422 for query validation; either accept that or normalize to 400 (your call)

Server errors

- 500 Internal Server Error
	- embedding or SQL failure

- 503 Service Unavailable
	- DB down

---

###GET /bins

Success

- 200 OK

Server errors

- 500, 503

---

###GET /bins/{bin_id}

Success

- 200 OK

Client errors

- 404 Not Found
	- bin doesn’t exist

Server errors

- 500, 503

---

##Suggested Mapping: Status → error.code

400 → bad_request

404 → not_found

409 → conflict

413 → payload_too_large

415 → unsupported_media_type

429 → rate_limited

500 → internal_error

503 → service_unavailable

