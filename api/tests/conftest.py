import importlib
import os
import sys
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


def _stub_fastembed() -> None:
    class DummyVec:
        def __init__(self, dims: int):
            self._dims = dims

        def tolist(self):
            return [0.1] * self._dims

    class DummyEmbed:
        def __init__(self, model_name: str):
            self.model_name = model_name

        def embed(self, texts):
            for _ in texts:
                yield DummyVec(384)

    sys.modules["fastembed"] = types.SimpleNamespace(TextEmbedding=DummyEmbed)


def _init_schema(engine) -> None:
    ddl = """
    CREATE EXTENSION IF NOT EXISTS vector;

    DROP TABLE IF EXISTS photo_detection_groups CASCADE;
    DROP TABLE IF EXISTS photo_detections CASCADE;
    DROP TABLE IF EXISTS photo_labels CASCADE;
    DROP TABLE IF EXISTS photo_group_items CASCADE;
    DROP TABLE IF EXISTS item_embeddings CASCADE;
    DROP TABLE IF EXISTS bin_items CASCADE;
    DROP TABLE IF EXISTS photos CASCADE;
    DROP TABLE IF EXISTS items CASCADE;
    DROP TABLE IF EXISTS bins CASCADE;

    CREATE TABLE bins (
      bin_id text PRIMARY KEY,
      deleted_at timestamptz,
      created_at timestamptz DEFAULT now()
    );

    CREATE TABLE items (
      item_id bigserial PRIMARY KEY,
      name text NOT NULL,
      category text,
      notes text,
      deleted_at timestamptz,
      fingerprint text GENERATED ALWAYS AS (
        lower(trim(name)) || '|' || coalesce(lower(trim(category)), '')
      ) STORED,
      created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX items_fingerprint_uq
    ON items (fingerprint);

    CREATE TABLE item_embeddings (
      item_id bigint PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
      model text NOT NULL,
      dims int NOT NULL,
      embedding vector(384) NOT NULL,
      updated_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE INDEX item_embeddings_hnsw_cos
    ON item_embeddings
    USING hnsw (embedding vector_cosine_ops);

    CREATE TABLE bin_items (
      id bigserial PRIMARY KEY,
      bin_id text REFERENCES bins(bin_id) ON DELETE CASCADE,
      item_id bigint REFERENCES items(item_id) ON DELETE CASCADE,
      confidence float,
      quantity float,
      created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS bin_items_unique
    ON bin_items (bin_id, item_id);

    CREATE TABLE photos (
      photo_id bigserial PRIMARY KEY,
      bin_id text REFERENCES bins(bin_id) ON DELETE CASCADE,
      path text NOT NULL,
      created_at timestamptz DEFAULT now()
    );

    CREATE TABLE photo_labels (
      id bigserial PRIMARY KEY,
      photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
      model text NOT NULL,
      label text NOT NULL,
      category text,
      confidence float NOT NULL,
      created_at timestamptz DEFAULT now()
    );

    CREATE TABLE photo_detections (
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

    CREATE TABLE photo_detection_groups (
      id bigserial PRIMARY KEY,
      photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
      model text NOT NULL,
      label text NOT NULL,
      category text,
      confidence_avg float NOT NULL,
      count_estimate int NOT NULL,
      created_at timestamptz DEFAULT now()
    );

    CREATE TABLE photo_group_items (
      id bigserial PRIMARY KEY,
      photo_id bigint REFERENCES photos(photo_id) ON DELETE CASCADE,
      model text NOT NULL,
      label text NOT NULL,
      category text,
      item_id bigint REFERENCES items(item_id) ON DELETE CASCADE,
      created_at timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX photo_group_items_uq
    ON photo_group_items (photo_id, model, label, category, item_id);
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))


@pytest.fixture(scope="session")
def app_module():
    test_db_url = os.environ.get("TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("TEST_DATABASE_URL not set")

    os.environ["DATABASE_URL"] = test_db_url
    os.environ.setdefault("PHOTO_DIR", "/tmp/binbrain_test_photos")

    _stub_fastembed()

    if "app.main" in sys.modules:
        del sys.modules["app.main"]

    import app.main as main
    importlib.reload(main)

    try:
        _init_schema(main.engine)
    except Exception as exc:
        pytest.skip(f"database setup failed: {exc}")

    return main


@pytest.fixture()
def client(app_module):
    return TestClient(app_module.app)


@pytest.fixture()
def db(app_module):
    db = app_module.SessionLocal()
    try:
        yield db
    finally:
        db.close()
