-- BinBrain database schema initialization
-- Runs automatically on first container startup via /docker-entrypoint-initdb.d
-- Database and role are created by the Docker image via POSTGRES_DB / POSTGRES_USER env vars.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS locations (
    location_id serial PRIMARY KEY,
    name        text NOT NULL,
    description text,
    parent_id   integer REFERENCES locations(location_id),
    deleted_at  timestamptz,
    created_at  timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS locations_name_uq
ON locations (lower(trim(name)))
WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS bins (
  bin_id text PRIMARY KEY,
  location_id integer REFERENCES locations(location_id),
  deleted_at timestamptz,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS items (
  item_id bigserial PRIMARY KEY,
  name text NOT NULL,
  category text,
  notes text,
  upc text,
  deleted_at timestamptz,
  fingerprint text GENERATED ALWAYS AS (
    lower(trim(name)) || '|' || coalesce(lower(trim(category)), '')
  ) STORED,
  created_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS items_fingerprint_uq
ON items (fingerprint);

CREATE UNIQUE INDEX IF NOT EXISTS items_upc_uq
ON items (upc)
WHERE upc IS NOT NULL;

CREATE INDEX IF NOT EXISTS items_upc_idx
ON items (upc)
WHERE upc IS NOT NULL AND deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS item_embeddings (
  item_id bigint PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
  model text NOT NULL,
  dims int NOT NULL,
  embedding vector(384) NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS item_embeddings_hnsw_cos
ON item_embeddings
USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS bin_items (
  id bigserial PRIMARY KEY,
  bin_id text REFERENCES bins(bin_id) ON DELETE CASCADE,
  item_id bigint REFERENCES items(item_id) ON DELETE CASCADE,
  confidence float,
  quantity float,
  created_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS bin_items_unique
ON bin_items (bin_id, item_id);

CREATE TABLE IF NOT EXISTS photos (
  photo_id bigserial PRIMARY KEY,
  bin_id text REFERENCES bins(bin_id) ON DELETE CASCADE,
  path text NOT NULL,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS photo_labels (
  id bigserial PRIMARY KEY,
  photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
  model text NOT NULL,
  label text NOT NULL,
  category text,
  confidence float NOT NULL,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS photo_detections (
  id bigserial PRIMARY KEY,
  photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
  model text NOT NULL,
  label text NOT NULL,
  category text,
  confidence float NOT NULL,
  x1 float NOT NULL,
  y1 float NOT NULL,
  x2 float NOT NULL,
  y2 float NOT NULL,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS photo_detection_groups (
  id bigserial PRIMARY KEY,
  photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
  model text NOT NULL,
  label text NOT NULL,
  category text,
  confidence_avg float NOT NULL,
  count_estimate int NOT NULL,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS photo_group_items (
  id bigserial PRIMARY KEY,
  photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
  model text NOT NULL,
  label text NOT NULL,
  category text,
  item_id bigint REFERENCES items(item_id) ON DELETE CASCADE,
  created_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS photo_group_items_uq
ON photo_group_items (photo_id, model, label, category, item_id);

CREATE TABLE IF NOT EXISTS api_keys (
    id          bigserial PRIMARY KEY,
    key_hash    text NOT NULL UNIQUE,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz,
    last_used   timestamptz
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

CREATE TABLE IF NOT EXISTS settings (
    key   text PRIMARY KEY,
    value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS confirmed_classes (
    id          bigserial PRIMARY KEY,
    class_name  text NOT NULL,
    category    text,
    source      text NOT NULL,
    confirmed_by text,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    removed_at   timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS confirmed_classes_name_uq
ON confirmed_classes (lower(trim(class_name)))
WHERE removed_at IS NULL;

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- Seed useful classes from COCO vocabulary
INSERT INTO confirmed_classes (class_name, category, source) VALUES
    ('scissors', 'tools', 'seed'),
    ('knife', 'tools', 'seed'),
    ('cell phone', 'electronics', 'seed'),
    ('laptop', 'electronics', 'seed'),
    ('remote', 'electronics', 'seed'),
    ('keyboard', 'electronics', 'seed'),
    ('mouse', 'electronics', 'seed'),
    ('bottle', 'household', 'seed'),
    ('cup', 'household', 'seed'),
    ('book', 'office', 'seed'),
    ('backpack', 'other', 'seed')
ON CONFLICT DO NOTHING;
