"""Re-export aggregator for the ``app.db`` package.

The implementation lives in domain submodules (``bins``, ``items``,
``photos``, ``sessions``, ``api_keys``, ``settings``, ``idempotency``,
``locations``, ``telemetry``, ``classes``). This module preserves the
historical ``from app.db import repository`` import surface so callers can
keep using ``repository.<func>`` without per-site rewrites.

The advisory-lock namespace registry below is load-bearing:
``test_advisory_lock_namespaces_match_repository_registry`` reads this
file directly and asserts that every ``pg_advisory_*(hashtext('<ns>'),
...)`` call site under ``api/app/`` is declared here.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Reserved advisory-lock namespaces (SEC-36-1)
#
# ``pg_advisory_xact_lock(int, int)`` and its bigint sibling are partitioned
# by whatever key the caller picks. Collisions across unrelated call sites
# cause silent serialization or — worse — silent under-protection. Every
# ``pg_advisory_*`` call in this codebase MUST add its namespace string here
# before landing. New sites should pick a unique, descriptive first argument
# (usually via ``hashtext('<namespace>')``) and document the combined key
# shape (first-arg + second-arg contract) on this list.
#
# Current namespaces:
#   ('session_create', api_key_id)
#     — ``create_session_if_under_cap`` — serializes concurrent
#       ``POST /sessions`` per api_key so the 20-open-session cap
#       cannot be breached under concurrent bursts. Scoped per-owner
#       so cross-owner session creation is NOT serialized.
#   ('idempotency', api_key_id)
#     — ``store_idempotent_response`` — serializes concurrent POSTs
#       that share an Idempotency-Key so exactly one txn runs the
#       underlying domain write and later arrivals read back the
#       stored response. Per-owner so cross-tenant idempotency never
#       interferes.
#
# CI guard (SEC-37-3): ``test_advisory_lock_namespaces_match_repository_registry``
# (in ``api/tests/test_sessions.py``) walks ``api/app/**/*.py``, extracts every
# ``pg_advisory_*(hashtext('<ns>'), ...)`` namespace, and asserts each one
# appears as ``('<ns>',`` in this comment block. Undeclared additions fail CI.
# ---------------------------------------------------------------------------
from app.db.api_keys import (
    create_api_key,
    hash_api_key,
    list_api_keys,
    revoke_api_key,
    touch_api_key_last_used,
    validate_api_key,
)
from app.db.bins import (
    _RESERVED_BIN_NAME_STEMS,
    MOVE_ERR_ITEM_NOT_FOUND_IN_SOURCE,
    MOVE_ERR_TARGET_ALREADY_HAS_ITEM,
    MOVE_ERR_TARGET_BIN_NOT_FOUND,
    UNASSIGNED_BIN_ID,
    _normalize_bin_name,
    bin_exists,
    ensure_bin_active_or_create,
    is_reserved_bin_name,
    list_bins,
    move_bin_item,
    reattribute_bin_items_to_unassigned,
    soft_delete_bin,
    update_bin_location,
)
from app.db.classes import (
    fetch_active_classes,
    insert_confirmed_class,
    soft_delete_class,
)
from app.db.idempotency import (
    IDEMPOTENCY_TTL_INTERVAL,
    IdempotencyKeyMismatch,
    acquire_idempotency_lock,
    fetch_idempotent_record,
    hash_canonical_body,
    store_idempotent_response,
)
from app.db.items import (
    _normalize_category,
    backfill_item_upc_if_missing,
    delete_bin_item,
    fetch_bin_items,
    find_item_by_upc,
    get_bin_item,
    insert_bin_item,
    insert_item,
    insert_item_with_status,
    insert_upc_lookup,
    search_items,
    search_items_by_embedding,
    update_bin_item,
    upsert_item_embedding,
)
from app.db.locations import (
    create_location,
    fetch_bin_location,
    get_location,
    list_locations,
    soft_delete_location,
    update_location,
)
from app.db.photos import (
    clear_detection_groups,
    clear_photo_detections,
    compute_groups_from_detections,
    delete_photo,
    fetch_bin_photos,
    fetch_cached_groups,
    fetch_photo_device_metadata,
    fetch_photo_groups,
    fetch_photo_path,
    find_photo_by_uuid,
    get_photo_detections,
    insert_detection_groups,
    insert_photo,
    insert_photo_detections,
    insert_photo_group_item,
    photo_exists,
)
from app.db.sessions import (
    _session_row_to_dict,
    count_open_sessions,
    create_session,
    create_session_if_under_cap,
    end_session,
    find_session,
    list_sessions,
    validate_session_for_ingest,
)
from app.db.settings import (
    get_setting,
    log_setting_change,
    set_setting,
)
from app.db.telemetry import (
    insert_photo_suggestion_matches,
    insert_search_query,
    insert_vision_call,
    link_suggestion_outcomes_to_item,
    replace_photo_suggestion_outcomes,
)

__all__ = [
    "IDEMPOTENCY_TTL_INTERVAL",
    "MOVE_ERR_ITEM_NOT_FOUND_IN_SOURCE",
    "MOVE_ERR_TARGET_ALREADY_HAS_ITEM",
    "MOVE_ERR_TARGET_BIN_NOT_FOUND",
    "UNASSIGNED_BIN_ID",
    "_RESERVED_BIN_NAME_STEMS",
    "IdempotencyKeyMismatch",
    "_normalize_bin_name",
    "_normalize_category",
    "_session_row_to_dict",
    "acquire_idempotency_lock",
    "backfill_item_upc_if_missing",
    "bin_exists",
    "clear_detection_groups",
    "clear_photo_detections",
    "compute_groups_from_detections",
    "count_open_sessions",
    "create_api_key",
    "create_location",
    "create_session",
    "create_session_if_under_cap",
    "delete_bin_item",
    "delete_photo",
    "end_session",
    "ensure_bin_active_or_create",
    "fetch_active_classes",
    "fetch_bin_items",
    "fetch_bin_location",
    "fetch_bin_photos",
    "fetch_cached_groups",
    "fetch_idempotent_record",
    "fetch_photo_device_metadata",
    "fetch_photo_groups",
    "fetch_photo_path",
    "find_item_by_upc",
    "find_photo_by_uuid",
    "find_session",
    "get_bin_item",
    "get_location",
    "get_photo_detections",
    "get_setting",
    "hash_api_key",
    "hash_canonical_body",
    "insert_bin_item",
    "insert_confirmed_class",
    "insert_detection_groups",
    "insert_item",
    "insert_item_with_status",
    "insert_photo",
    "insert_photo_detections",
    "insert_photo_group_item",
    "insert_photo_suggestion_matches",
    "insert_search_query",
    "insert_upc_lookup",
    "insert_vision_call",
    "is_reserved_bin_name",
    "link_suggestion_outcomes_to_item",
    "list_api_keys",
    "list_bins",
    "list_locations",
    "list_sessions",
    "log_setting_change",
    "move_bin_item",
    "photo_exists",
    "reattribute_bin_items_to_unassigned",
    "replace_photo_suggestion_outcomes",
    "revoke_api_key",
    "search_items",
    "search_items_by_embedding",
    "set_setting",
    "soft_delete_bin",
    "soft_delete_class",
    "soft_delete_location",
    "store_idempotent_response",
    "touch_api_key_last_used",
    "update_bin_item",
    "update_bin_location",
    "update_location",
    "upsert_item_embedding",
    "validate_api_key",
    "validate_session_for_ingest",
]
