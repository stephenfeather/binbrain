# BinBrain – Project Handoff Document
##Overview

BinBrain is a Raspberry Pi–hosted inventory system designed to:

- Uniquely identify storage bins
- Associate bins with items
- Generate text embeddings for items
- Store embeddings in PostgreSQL using pgvector
- Enable semantic search across stored items
- Support photo ingestion for future vision classification

Current deployment target:

- Raspberry Pi 5 8gb
- Native PostgreSQL 17 with pgvector (vector 0.8.0)
- FastAPI application running in Docker
- Text embeddings via fastembed using BAAI/bge-small-en-v1.5
- Embedding dimension: 384


##System Architecture
###Host (Raspberry Pi)

Native PostgreSQL 17

pgvector extension enabled

Database: binbrain

Role: binbrain (password-authenticated)

###Docker

Single container:

- Service: binbrain_api
- Framework: FastAPI
- ORM: SQLAlchemy
- DB Driver: psycopg (SQLAlchemy 2.x)
- Embeddings: fastembed (CPU)

Docker network mode: host
API binds to: http://0.0.0.0:8000

##Database Schema

###Extensions
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```
--
###bins

```sql
CREATE TABLE bins (
  bin_id text PRIMARY KEY,
  created_at timestamptz DEFAULT now()
);
```
--
###items

```sql
CREATE TABLE items (
  item_id bigserial PRIMARY KEY,
  name text NOT NULL,
  category text,
  notes text,
  fingerprint text NOT NULL,
  created_at timestamptz DEFAULT now()
);
```

###Uniqueness Enforcement

```sql
CREATE UNIQUE INDEX items_fingerprint_uq
ON items (fingerprint);
```
Fingerprint logic:

```sql
lower(trim(name)) || '|' || coalesce(lower(trim(category)), '')
```

Duplicates are prevented at the DB level.

--
###item_embeddings

```sql
CREATE TABLE item_embeddings (
  item_id bigint PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
  model text NOT NULL,
  dims int NOT NULL,
  embedding vector(384) NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

Index:

```sql
CREATE INDEX item_embeddings_hnsw_cos
ON item_embeddings
USING hnsw (embedding vector_cosine_ops);
```
--
###bin_items
```sql
CREATE TABLE bin_items (
  id bigserial PRIMARY KEY,
  bin_id text REFERENCES bins(bin_id) ON DELETE CASCADE,
  item_id bigint REFERENCES items(item_id) ON DELETE CASCADE,
  confidence float,
  quantity float,
  created_at timestamptz DEFAULT now()
);
```
###photos

```sql
CREATE TABLE photos (
  photo_id bigserial PRIMARY KEY,
  bin_id text REFERENCES bins(bin_id) ON DELETE CASCADE,
  path text NOT NULL,
  created_at timestamptz DEFAULT now()
);
```
--
##API Endpoints
###GET /health

Returns:

```json
{
  "ok": true,
  "embed_model": "BAAI/bge-small-en-v1.5",
  "expected_dims": 384
}
```
--
###POST /items

Behavior:

- Computes fingerprint
- INSERT ... ON CONFLICT (fingerprint)
- Upserts embedding
- Optionally associates to bin
- Atomic transaction

Form fields:

- name (required)
- category (optional)
- notes (optional)
- bin_id (optional)
- confidence (optional)
- quantity (optional)

Returns:

```json
{
  "item_id": 6,
  "fingerprint": "m3 socket head cap screw 12mm|fastener",
  "name": "...",
  "category": "...",
  "notes": "...",
  "bin_id": "BIN-0001"
}
```

Calling this twice with same name+category returns same item_id.

--
###GET /search?q=...&limit=...

- Embeds query

- Uses cosine distance operator <=>

- Returns items ordered by similarity

- Includes list of bins per item
--

###POST /associate

Associates an existing item_id to a bin.

--

###POST /ingest

Uploads photos for a bin.

Stores files at:

`/data/photos/<bin_id>/`

No classification yet.

--
###Embeddings

Model:

`BAAI/bge-small-en-v1.5`

Embedding size:

`384 dimensions`

Vector stored as:

`vector(384)`

Binding strategy:

We use:

`CAST(:embedding AS vector)`

NOT:

`:embedding::vector`

This avoids SQLAlchemy bind parsing issues.

--
###Environment Variables

Required:

```code
DATABASE_URL=postgresql+psycopg://binbrain:<password>@127.0.0.1:5432/binbrain
PHOTO_DIR=/data/photos
EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIMS=384
```

--
###Known Design Decisions

1. Native Postgres instead of containerized Postgres
2. Vector index uses HNSW cosine
3. Embeddings stored per item (not per bin)
4. Fingerprint enforces logical uniqueness
5. API is atomic — no orphan items

##Known Future Work

###1. Vision Integration (Hailo 26 TOPS Hat+)

Planned:

- Host-level inference service
- Detection/classification of bin photos
- Auto-suggest items
- Store results in photo_labels table

--
###2. Bin View Endpoint

Needed:

`GET /bins/{bin_id}`

Should return:

- items
- quantities
- photos

--
###3. QR Label Generator

Future service:

- CSV export
- QR codes
- Avery sheet layout
- Label printer support

###4. iPhone Shortcut Endpoint

Desired shape:

`POST /bins/{bin_id}/add`

- photo(s)
- optional name/category
- automatic embedding
- single call workflow

##Operational Notes

- Docker uses host networking
- Postgres runs natively
- Permissions for role binbrain granted on all tables + sequences
- Unique index prevents duplicates permanently
- Duplicate cleanup already completed

##Current State

System is:

- Running
- Search functional
- Embedding functional
- Duplicate-safe
- Vector index operational

This is a stable base.