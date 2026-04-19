import hashlib
import importlib
import os
import secrets
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


# Data tables that tests mutate. Truncated between tests so ordering and
# fixture re-use do not leak state. ``api_keys``, ``settings``, and
# ``confirmed_classes`` are EXCLUDED because they are populated by
# session-scope fixtures or app startup and must survive across tests.
_TRUNCATE_BETWEEN_TESTS_SQL = (
    "TRUNCATE search_queries, photo_suggestion_matches, vision_calls, "
    "photo_suggestion_outcomes, photo_group_items, "
    "photo_detection_groups, photo_detections, photo_labels, "
    "item_upc_lookups, "
    "item_embeddings, bin_items, photos, sessions, items, bins, "
    "locations RESTART IDENTITY CASCADE"
)

# Session-end cleanup: wipe everything including api_keys so re-runs start
# from the same state as the very first run. (Problem A acceptance:
# SELECT COUNT(*) FROM api_keys post-run == pre-run.)
_TRUNCATE_ALL_SQL = (
    "TRUNCATE search_queries, photo_suggestion_matches, vision_calls, "
    "photo_suggestion_outcomes, photo_group_items, "
    "photo_detection_groups, photo_detections, photo_labels, "
    "item_upc_lookups, "
    "item_embeddings, bin_items, photos, sessions, items, bins, "
    "locations, settings, confirmed_classes, api_keys RESTART IDENTITY CASCADE"
)


def _init_schema(engine) -> None:
    ddl = """
    CREATE EXTENSION IF NOT EXISTS vector;

    DROP TABLE IF EXISTS item_upc_lookups CASCADE;
    DROP TABLE IF EXISTS search_queries CASCADE;
    DROP TABLE IF EXISTS photo_suggestion_matches CASCADE;
    DROP TABLE IF EXISTS vision_calls CASCADE;
    DROP TABLE IF EXISTS photo_suggestion_outcomes CASCADE;
    DROP TABLE IF EXISTS photo_detection_groups CASCADE;
    DROP TABLE IF EXISTS photo_detections CASCADE;
    DROP TABLE IF EXISTS photo_labels CASCADE;
    DROP TABLE IF EXISTS photo_group_items CASCADE;
    DROP TABLE IF EXISTS item_embeddings CASCADE;
    DROP TABLE IF EXISTS bin_items CASCADE;
    DROP TABLE IF EXISTS sessions CASCADE;
    DROP TABLE IF EXISTS photos CASCADE;
    DROP TABLE IF EXISTS items CASCADE;
    DROP TABLE IF EXISTS bins CASCADE;
    DROP TABLE IF EXISTS locations CASCADE;

    CREATE TABLE locations (
        location_id serial PRIMARY KEY,
        name        text NOT NULL,
        description text,
        parent_id   integer REFERENCES locations(location_id),
        deleted_at  timestamptz,
        created_at  timestamptz DEFAULT now()
    );

    CREATE UNIQUE INDEX locations_name_uq
    ON locations (lower(trim(name)))
    WHERE deleted_at IS NULL;

    CREATE TABLE bins (
      bin_id text PRIMARY KEY,
      location_id integer REFERENCES locations(location_id),
      deleted_at timestamptz,
      created_at timestamptz DEFAULT now()
    );

    -- FEAT-3: protect the UNASSIGNED sentinel from accidental DELETE or
    -- soft-delete (UPDATE setting deleted_at). Mirrors
    -- migrations/2026-04-18_add_unassigned_bin_sentinel.sql.
    CREATE OR REPLACE FUNCTION protect_unassigned_bin()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF TG_OP = 'DELETE' AND OLD.bin_id = 'UNASSIGNED' THEN
            RAISE EXCEPTION 'cannot delete sentinel UNASSIGNED bin'
                USING ERRCODE = 'check_violation';
        END IF;
        IF TG_OP = 'UPDATE' AND OLD.bin_id = 'UNASSIGNED' THEN
            IF NEW.deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'cannot soft-delete sentinel UNASSIGNED bin'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.bin_id <> OLD.bin_id THEN
                RAISE EXCEPTION 'cannot rename sentinel UNASSIGNED bin'
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$;

    DROP TRIGGER IF EXISTS bins_protect_unassigned ON bins;
    CREATE TRIGGER bins_protect_unassigned
        BEFORE UPDATE OR DELETE ON bins
        FOR EACH ROW
        EXECUTE FUNCTION protect_unassigned_bin();

    INSERT INTO bins (bin_id) VALUES ('UNASSIGNED')
    ON CONFLICT (bin_id) DO NOTHING;

    CREATE TABLE items (
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

    CREATE UNIQUE INDEX items_fingerprint_uq
    ON items (fingerprint);

    CREATE UNIQUE INDEX items_upc_uq
    ON items (upc)
    WHERE upc IS NOT NULL;

    CREATE INDEX items_upc_idx
    ON items (upc)
    WHERE upc IS NOT NULL AND deleted_at IS NULL;

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
      device_metadata jsonb,
      width integer,
      height integer,
      session_id text,
      photo_uuid uuid NOT NULL DEFAULT gen_random_uuid(),
      created_at timestamptz DEFAULT now()
    );
    CREATE UNIQUE INDEX photos_photo_uuid_uq ON photos (photo_uuid);

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
      prompt_version text,
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

    CREATE TABLE photo_suggestion_outcomes (
      id               bigserial PRIMARY KEY,
      photo_id         bigint NOT NULL REFERENCES photos(photo_id) ON DELETE CASCADE,
      vision_model     text   NOT NULL,
      prompt_version   text,
      label            text   NOT NULL,
      category         text,
      confidence       double precision,
      bbox             double precision[],
      shown_at         timestamptz NOT NULL,
      decision         text   NOT NULL
                       CHECK (decision IN ('accepted', 'rejected', 'edited', 'ignored')),
      edited_to_label  text,
      decided_at       timestamptz NOT NULL DEFAULT now(),
      item_id          bigint REFERENCES items(item_id) ON DELETE SET NULL,
      client_retry_count int
    );
    CREATE INDEX photo_suggestion_outcomes_photo_idx
        ON photo_suggestion_outcomes (photo_id);
    CREATE INDEX photo_suggestion_outcomes_decision_idx
        ON photo_suggestion_outcomes (decision);
    CREATE INDEX photo_suggestion_outcomes_photo_model_idx
        ON photo_suggestion_outcomes (photo_id, vision_model);
    CREATE INDEX photo_suggestion_outcomes_item_idx
        ON photo_suggestion_outcomes (item_id)
        WHERE item_id IS NOT NULL;

    CREATE TABLE vision_calls (
      id             bigserial PRIMARY KEY,
      photo_id       bigint REFERENCES photos(photo_id) ON DELETE SET NULL,
      model          text NOT NULL,
      prompt_version text,
      base_url       text,
      started_at     timestamptz NOT NULL,
      elapsed_ms     integer,
      hits_count     integer,
      cached         boolean NOT NULL,
      outcome        text NOT NULL CHECK (outcome IN ('ok','error')),
      error_code     text,
      flags          jsonb NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX vision_calls_started_at_idx ON vision_calls (started_at);
    CREATE INDEX vision_calls_model_idx      ON vision_calls (model);
    CREATE INDEX vision_calls_outcome_idx    ON vision_calls (outcome);
    CREATE INDEX vision_calls_photo_idx      ON vision_calls (photo_id);

    CREATE TABLE photo_suggestion_matches (
      id                    bigserial PRIMARY KEY,
      photo_detection_id    bigint NOT NULL
                            REFERENCES photo_detections(id) ON DELETE CASCADE,
      matched_item_id       bigint REFERENCES items(item_id) ON DELETE SET NULL,
      score                 double precision NOT NULL,
      threshold_at_compute  double precision NOT NULL,
      computed_at           timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX photo_suggestion_matches_detection_idx
        ON photo_suggestion_matches (photo_detection_id);
    CREATE INDEX photo_suggestion_matches_item_idx
        ON photo_suggestion_matches (matched_item_id);

    CREATE TABLE search_queries (
      id                    bigserial PRIMARY KEY,
      request_id            text,
      q                     text NOT NULL,
      qvec_dims             integer NOT NULL,
      min_score_effective   double precision NOT NULL,
      result_count          integer NOT NULL,
      created_at            timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX search_queries_created_at_idx
        ON search_queries (created_at);
    CREATE INDEX search_queries_result_created_idx
        ON search_queries (result_count, created_at DESC);

    CREATE TABLE item_upc_lookups (
      id            bigserial PRIMARY KEY,
      upc           text NOT NULL,
      item_id       bigint REFERENCES items(item_id) ON DELETE SET NULL,
      source        text NOT NULL
                    CHECK (source IN ('local', 'upcitemdb', 'go-upc', 'unknown')),
      raw_response  jsonb,
      elapsed_ms    integer,
      created_at    timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX item_upc_lookups_upc_idx
        ON item_upc_lookups (upc, created_at DESC);
    CREATE INDEX item_upc_lookups_item_id_idx
        ON item_upc_lookups (item_id)
        WHERE item_id IS NOT NULL;
    CREATE INDEX item_upc_lookups_source_created_idx
        ON item_upc_lookups (source, created_at DESC);

    CREATE OR REPLACE VIEW item_upc_lookups_metadata AS
    SELECT id, item_id, upc, source, elapsed_ms, created_at
    FROM item_upc_lookups;

    DROP TABLE IF EXISTS api_keys CASCADE;
    CREATE TABLE api_keys (
        id          bigserial PRIMARY KEY,
        key_hash    text NOT NULL UNIQUE,
        name        text NOT NULL,
        role        text NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
        created_at  timestamptz NOT NULL DEFAULT now(),
        revoked_at  timestamptz,
        last_used   timestamptz
    );
    CREATE INDEX idx_api_keys_hash ON api_keys(key_hash);

    -- ApiDev_008 (Q-session-id): mirror migrations/2026-04-19_add_sessions_table.sql
    -- Must follow api_keys (FK target) and photos (trigger attaches here).
    CREATE TABLE sessions (
        session_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        api_key_id  bigint NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
        started_at  timestamptz NOT NULL DEFAULT now(),
        ended_at    timestamptz,
        label       text,
        photo_count int NOT NULL DEFAULT 0
    );
    CREATE INDEX sessions_api_key_idx
        ON sessions (api_key_id, started_at DESC);
    CREATE INDEX sessions_open_idx
        ON sessions (api_key_id) WHERE ended_at IS NULL;

    CREATE OR REPLACE FUNCTION sessions_update_photo_count()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE
        sid uuid;
    BEGIN
        IF TG_OP = 'INSERT' AND NEW.session_id IS NOT NULL AND NEW.session_id <> '' THEN
            BEGIN
                sid := NEW.session_id::uuid;
                -- ApiDev_008b (F-5): skip bump if the session was closed
                -- between /ingest's validate and the photo insert. Photo
                -- row still lands; only the count is race-safe.
                UPDATE sessions
                SET photo_count = photo_count + 1
                WHERE session_id = sid
                  AND ended_at IS NULL;
            EXCEPTION WHEN invalid_text_representation THEN
                NULL;
            END;
        ELSIF TG_OP = 'DELETE' AND OLD.session_id IS NOT NULL AND OLD.session_id <> '' THEN
            BEGIN
                sid := OLD.session_id::uuid;
                UPDATE sessions
                SET photo_count = GREATEST(photo_count - 1, 0)
                WHERE session_id = sid;
            EXCEPTION WHEN invalid_text_representation THEN
                NULL;
            END;
        END IF;
        RETURN COALESCE(NEW, OLD);
    END;
    $$;

    DROP TRIGGER IF EXISTS photos_update_session_photo_count ON photos;
    CREATE TRIGGER photos_update_session_photo_count
        AFTER INSERT OR DELETE ON photos
        FOR EACH ROW
        EXECUTE FUNCTION sessions_update_photo_count();

    DROP TABLE IF EXISTS settings CASCADE;
    CREATE TABLE settings (
        key   text PRIMARY KEY,
        value text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    );

    DROP TABLE IF EXISTS confirmed_classes CASCADE;
    CREATE TABLE confirmed_classes (
        id          bigserial PRIMARY KEY,
        class_name  text NOT NULL,
        category    text,
        source      text NOT NULL,
        confirmed_by text,
        confirmed_at timestamptz NOT NULL DEFAULT now(),
        removed_at   timestamptz
    );
    CREATE UNIQUE INDEX confirmed_classes_name_uq
    ON confirmed_classes (lower(trim(class_name)))
    WHERE removed_at IS NULL;
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))


# Ensure this directory is on sys.path so sibling modules (e.g. _db_guard) are
# importable during conftest evaluation, before pytest's usual rootdir setup.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _db_guard import test_db_isolation_error as _test_db_isolation_error  # noqa: E402


@pytest.fixture(scope="session")
def app_module():
    test_db_url = os.environ.get("TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("TEST_DATABASE_URL not set")

    # Dev2_013 Problem B: fail-fast if the test URL is in any way unsafe
    # (points at prod, missing 'test' marker, coordination DB, etc.) before
    # any schema drop executes.
    err = _test_db_isolation_error(test_db_url, os.environ.get("DATABASE_URL"))
    if err:
        pytest.fail(err)

    os.environ["DATABASE_URL"] = test_db_url
    os.environ.setdefault("PHOTO_DIR", "/tmp/binbrain_test_photos")

    _stub_fastembed()

    for mod in [m for m in sys.modules if m.startswith("app.")]:
        del sys.modules[mod]

    import app.deps as deps
    import app.main as main

    importlib.reload(deps)
    importlib.reload(main)

    try:
        _init_schema(deps.engine)
    except Exception as exc:
        pytest.skip(f"database setup failed: {exc}")

    # Stub detection so integration tests don't need real YOLOE weights.
    # Patch on the route module because it binds these at import time.
    import app.routes.photos as photos_route

    photos_route.detect = lambda photo_path: []
    photos_route.get_model_name = lambda: "stub"

    yield main

    # Session teardown (Dev2_013 Problem A): leave the test DB clean so back-
    # to-back pytest runs start from the same state they left. Best-effort —
    # don't mask real test failures if TRUNCATE itself raises.
    try:
        with deps.engine.begin() as conn:
            conn.execute(text(_TRUNCATE_ALL_SQL))
    except Exception:
        pass


@pytest.fixture(scope="session")
def test_api_key(app_module):
    """Create a test API key and return the raw key string."""
    from app.deps import SessionLocal

    raw_key = "bb_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    db = SessionLocal()
    try:
        db.execute(
            text("INSERT INTO api_keys (key_hash, name, role) VALUES (:key_hash, :name, 'admin')"),
            {"key_hash": key_hash, "name": "test-fixture"},
        )
        db.commit()
    finally:
        db.close()
    return raw_key


@pytest.fixture()
def client(app_module, test_api_key):
    c = TestClient(app_module.app)
    c.headers["X-API-Key"] = test_api_key
    return c


@pytest.fixture(scope="session")
def user_api_key(app_module):
    """Create a role='user' API key for tests that need non-admin rate limits."""
    from app.deps import SessionLocal
    from sqlalchemy import text

    raw_key = "bb_user_" + secrets.token_urlsafe(24)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    db = SessionLocal()
    try:
        db.execute(
            text("INSERT INTO api_keys (key_hash, name, role) VALUES (:key_hash, :name, 'user')"),
            {"key_hash": key_hash, "name": "test-fixture-user"},
        )
        db.commit()
    finally:
        db.close()
    return raw_key


@pytest.fixture()
def user_client(app_module, user_api_key):
    """TestClient authenticated as a role='user' key (no admin 4× rate-limit multiplier)."""
    c = TestClient(app_module.app)
    c.headers["X-API-Key"] = user_api_key
    return c


@pytest.fixture()
def db(app_module):
    from app.deps import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="session")
def valid_jpeg_bytes():
    """Real 1×1 JPEG produced by Pillow — passes magic-byte + PIL.verify() checks."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (1, 1), "red").save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _truncate_mutable_tables_between_tests(request):
    """Reset mutable app tables before every DB-touching test (Dev2_013 Problem A).

    Excludes ``api_keys`` so the session-scope ``test_api_key`` /
    ``user_api_key`` fixtures survive across the whole run. Pure unit tests
    (no ``client`` / ``db`` / ``app_module`` / ``user_client`` in the fixture
    graph) skip the TRUNCATE entirely so they stay independent of DB setup.
    """
    db_fixture_names = {"app_module", "client", "user_client", "db"}
    if not db_fixture_names & set(request.fixturenames):
        return
    from app.deps import engine

    with engine.begin() as conn:
        conn.execute(text(_TRUNCATE_BETWEEN_TESTS_SQL))
        # FEAT-3: TRUNCATE wipes the sentinel; re-seed it before any test
        # exercises bin lookups. INSERT bypasses the protect_unassigned_bin
        # trigger (it only fires on UPDATE/DELETE).
        conn.execute(text("INSERT INTO bins (bin_id) VALUES ('UNASSIGNED') ON CONFLICT DO NOTHING"))


@pytest.fixture(autouse=True)
def reset_rate_limiters():
    """Reset all in-process rate limiter state before each test.

    Without this, the 120/min global budget can be exhausted by earlier
    integration tests (all using the same test API key), causing subsequent
    tests to receive spurious 429s before their own rate-limit assertions run.

    Also ensures the per-endpoint swap pattern used by F-08 tests starts from
    a clean slate regardless of test execution order.

    No app_module dependency: rate_limiter has no DB imports and is safe to
    import at any point in the test session, including during pure unit tests.
    """
    try:
        from app.services import rate_limiter

        rate_limiter.global_limiter.reset()
        rate_limiter.vision_limiter.reset()
        rate_limiter.warmup_limiter.reset()
        rate_limiter.upc_limiter.reset()
    except (ImportError, AttributeError):
        pass  # Rate limiter not yet loaded — nothing to reset
