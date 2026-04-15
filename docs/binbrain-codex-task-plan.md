#BinBrain – Codex Task Planning Document

##Purpose

This document defines:

- Immediate implementation tasks
- Refactor tasks
- Architectural next steps
- Guardrails (what must NOT be changed)
- Acceptance criteria per task

Codex should use this as the authoritative roadmap for structured incremental improvements.

##Guardrails (Do Not Break These)

1. PostgreSQL runs natively on host
2. pgvector dimension is 384
3. Embedding model: BAAI/bge-small-en-v1.5
4. Unique index on items.fingerprint must remain enforced
5. API must remain atomic for /items
6. Use CAST(:param AS vector) — NOT :param::vector
7. Do not change database host/connection structure

##Current System Snapshot

- FastAPI
- SQLAlchemy 2.x style
- psycopg driver
- pgvector with HNSW cosine index
- Docker host networking
- Raspberry Pi 5 deployment target

Working endpoints:

- /health
- /items (upsert + embedding)
- /search
- /associate
- /ingest

System is stable.

##Phase 1 – API Enhancements (Safe Improvements)
###Task 1 – Add GET /bins/{bin_id}
####Goal

Return full bin contents including:

- items
- quantities
- confidence
- photo paths

####Expected Response Shape
```json
{
  "bin_id": "BIN-0001",
  "items": [
    {
      "item_id": 6,
      "name": "M3 socket head cap screw 12mm",
      "category": "fastener",
      "quantity": 25,
      "confidence": 0.95
    }
  ],
  "photos": [
    {
      "photo_id": 3,
      "path": "/data/photos/BIN-0001/abc.jpg"
    }
  ]
}
```
####Acceptance Criteria

- Returns 404 if bin does not exist
- No N+1 queries
- Single SQL query for items
- Separate SQL query for photos
- No schema changes

###Task 2 – Add GET /bins
####Goal

Return list of bins with metadata.

####Response Shape
```json
[
  {
    "bin_id": "BIN-0001",
    "item_count": 3,
    "photo_count": 2,
    "last_updated": "timestamp"
  }
]
```

Acceptance:

- Uses aggregate querie
- Sorted by most recently updated

##Phase 2 – Stability & Data Integrity
###Task 3 – Add DB Constraint for bin_items Uniqueness

Prevent duplicate bin associations:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS bin_items_unique
ON bin_items (bin_id, item_id);
```

Then modify `/items` and `/associate` to use:

`ON CONFLICT DO NOTHING`

Acceptance:

- Cannot insert duplicate (bin_id, item_id)
- No API failure on duplicate association

###Task 4 – Add Soft Delete Support (Optional)

Add deleted_at column to:

- items
- bins

Modify queries to filter where deleted_at IS NULL.

Do NOT implement unless requested.

##Phase 3 – iPhone Shortcut Optimization
###Task 5 – Add Unified Endpoint

Create:

`POST /bins/{bin_id}/add`

Behavior:

- Accept:

	- optional name
	- optional category
	- optional notes
	- optional photo(s)

- If name provided → upsert item
- If photos provided → store photos
- If both → associate automatically

Acceptance:

- One call workflow
- Fully atomic
- No partial inserts

##Phase 4 – Vision Integration (Planned, Not Implemented)
###Task 6 – Add photo_labels Table

Schema:

```sql
CREATE TABLE photo_labels (
  id bigserial PRIMARY KEY,
  photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
  model text NOT NULL,
  label text NOT NULL,
  confidence float NOT NULL,
  created_at timestamptz DEFAULT now()
);
```

Do NOT integrate Hailo yet. User will inform when hardware installed.

##Phase 5 – Performance Improvements
###Task 7 – Add Pagination to /search

Parameters:
- offset
- limit

Must maintain ordering by cosine distance.

###Task 8 – Add Top-K Filter Threshold

Optional query param:

`min_score`

Filter based on similarity threshold.

##Refactor Opportunities (Safe)
###Refactor 1 – Move DB Logic to Repository Layer

Split:

- main.py
- db/repository.py
- services/embedding.py

Goal:

- Separation of concerns
- Cleaner testing
- Do NOT change behavior.

###Refactor 2 – Add Structured Logging

Replace prints with structured logging:

- include request id
- include bin_id where applicable
- include item_id where applicable

##Testing Strategy

Codex should:

1. Add unit tests for:
	- fingerprint generation
	- embedding dimension validation
	- upsert logic
2. Add integration test:
	- Create item twice
	- Verify only one row in items

##Migration Policy

When altering schema:

1. Provide SQL migration
2. Do NOT drop tables without confirmation
3. Maintain backward compatibility
4. Never change embedding dimension silently

##Production Hardening Tasks (Later)

- Add rate limiting
- Add API key authentication
- Add request size limits for photo uploads
- Add health check for DB connectivity
- Add structured error responses

##What Codex Should Never Do

- Change embedding model without updating dimension
- Change fingerprint format
- Change pgvector operator
- Remove uniqueness constraints
- Change transaction semantics of /items

##Success Definition

System must:

- Prevent duplicates permanently
- Return consistent item_id for same logical item
- Support semantic search across bins
- Remain stable under repeated identical POST requests
- Remain compatible with Raspberry Pi 5 constraints

##Priority Order for Implementation

1. GET /bins/{bin_id}
2. GET /bins
3. bin_items unique constraint
4. Unified add endpoint
5. Pagination on search
6. Vision scaffolding

