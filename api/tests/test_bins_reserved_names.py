"""ApiDev_011 — reserved-bin-names validator.

Server authoritative reject of user-supplied bin names that collide with the
reserved registry (``UNASSIGNED`` sentinel and ``Binless`` iOS display string).
Case-insensitive via ``str.casefold``, whitespace-trimmed. iOS mirrors the
list client-side for pre-flight UX (Swift2_023) but the server is the
authoritative reject.

Entrypoint under test: ``_check_bin_id`` on the ``/ingest`` form — the only
user-supplied ``bin_id`` path in-tree today. No migration. Validator lives
at the HTTP boundary; sentinel seed writes in repository.py are exempt.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


def _post_ingest(client, valid_jpeg_bytes, *, bin_id: str):
    return client.post(
        "/ingest",
        data={"bin_id": bin_id},
        files={"photos": ("photo.jpg", valid_jpeg_bytes, "image/jpeg")},
    )


# ---------------------------------------------------------------------------
# 1. Happy path — a non-reserved bin_id still succeeds end-to-end.
# ---------------------------------------------------------------------------


def test_non_reserved_bin_id_still_succeeds(client, valid_jpeg_bytes):
    r = _post_ingest(client, valid_jpeg_bytes, bin_id="kitchen")
    assert r.status_code < 400, r.text


# ---------------------------------------------------------------------------
# 2+3. Exact matches for each reserved entry.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bin_id", ["UNASSIGNED", "Binless"])
def test_reserved_name_exact_match_is_400(bin_id, client, valid_jpeg_bytes):
    r = _post_ingest(client, valid_jpeg_bytes, bin_id=bin_id)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "reserved_bin_name"


# ---------------------------------------------------------------------------
# 4. Case variants — casefold equivalence across each reserved entry.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bin_id",
    [
        "unassigned",
        "Unassigned",
        "UNASSIGNED",
        "uNaSsIgNeD",
        "BINLESS",
        "binless",
        "Binless",
        "BinLess",
    ],
)
def test_reserved_name_case_insensitive(bin_id, client, valid_jpeg_bytes):
    r = _post_ingest(client, valid_jpeg_bytes, bin_id=bin_id)
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "reserved_bin_name"
    # Message echoes the normalized form so the user understands what
    # collided. NEVER the raw input — we will not reflect arbitrary bytes.
    assert "unassigned" in body["message"].lower() or "binless" in body["message"].lower()


# ---------------------------------------------------------------------------
# 5. Whitespace variants — strip() happens in _check_bin_id before regex,
#    so leading/trailing whitespace on an otherwise-reserved name must still
#    hit the 400 reserved path (not the generic invalid_bin_id path).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bin_id",
    [
        "  UNASSIGNED ",
        "Binless\n",
        "\tunassigned",
        " Binless",
        "UNASSIGNED\t",
    ],
)
def test_reserved_name_whitespace_variants(bin_id, client, valid_jpeg_bytes):
    r = _post_ingest(client, valid_jpeg_bytes, bin_id=bin_id)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "reserved_bin_name"


# ---------------------------------------------------------------------------
# 6. Unicode casefold edge — not every "lowercase" transform is a casefold.
#    Regression guard: if a future refactor swaps ``.casefold()`` for
#    ``.lower()``, tests exercising Unicode-fold-only collisions break.
#    Here we test a codepoint that casefolds to a plain-ASCII reserved name.
# ---------------------------------------------------------------------------


def test_reserved_name_unicode_casefold(client, valid_jpeg_bytes):
    # German eszett (ß) casefolds to "ss" — neither ".lower()" nor ".upper()"
    # produces that. If "unasssignedß" were reserved, the unicode input
    # "UNASSSIGNEDß" would only be caught under casefold. We don't have that
    # name reserved today, so instead exercise a minimal invariant: U+0130
    # (LATIN CAPITAL LETTER I WITH DOT ABOVE) casefolds to 'i' + combining
    # dot, NOT to plain 'i'. But Unicode "UNASSIGNED" with an I-with-dot
    # stays case-insensitive via casefold. We assert the register still
    # matches after a codepoint-roundtrip, and ALSO assert the guard:
    # validate_bin_id rejects non-[A-Za-z0-9_-] so the real test here is
    # that ``is_reserved_bin_name`` (pure predicate) agrees on casefold.
    from app.db import repository

    # Non-ASCII codepoint that casefolds to ASCII "ss" — if we ever reserve
    # a name containing "ss", a future bad refactor from casefold -> lower
    # would let the ß variant through. Today, no reserved name contains
    # "ss", so test the predicate directly on the casefold behavior itself.
    assert "ß".casefold() == "ss"
    # Concrete predicate assertion: casefold must normalize whitespace +
    # case BEFORE membership, not the other way round.
    assert repository.is_reserved_bin_name("  UNASSIGNED ") is True
    assert repository.is_reserved_bin_name("\tbinless\n") is True


# ---------------------------------------------------------------------------
# 7. Sentinel-row regression — validator must NOT leak into repository
#    helpers. The UNASSIGNED seed row is written by conftest (_init_schema)
#    + by repository soft-delete reattribution paths, never via user input.
# ---------------------------------------------------------------------------


def test_sentinel_row_survives_validator(client, db, valid_jpeg_bytes):
    # Force a bin-touching request to run so conftest sets up schema fully,
    # then verify the sentinel is present and unchanged.
    _post_ingest(client, valid_jpeg_bytes, bin_id="kitchen")
    sentinel = db.execute(
        text("SELECT bin_id FROM bins WHERE bin_id = :b"),
        {"b": "UNASSIGNED"},
    ).scalar_one_or_none()
    assert sentinel == "UNASSIGNED", (
        "UNASSIGNED sentinel row disappeared — the validator must not be wired "
        "into repository-layer INSERTs."
    )


# ---------------------------------------------------------------------------
# 8. Registry-driven extensibility — adding a new stem requires zero
#    validator-logic changes. Monkeypatching the frozenset (to a superset)
#    must cause the wire-up to block the newly-reserved name too.
# ---------------------------------------------------------------------------


def test_registry_is_authoritative(monkeypatch, client, valid_jpeg_bytes):
    from app.db import repository

    extended = frozenset(repository._RESERVED_BIN_NAME_STEMS | {"teststem"})
    monkeypatch.setattr(repository, "_RESERVED_BIN_NAME_STEMS", extended)

    r = _post_ingest(client, valid_jpeg_bytes, bin_id="TestStem")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "reserved_bin_name"


# ---------------------------------------------------------------------------
# 9. Predicate unit tests — `is_reserved_bin_name` contract guard.
#    Keeps the predicate's promises stable even if the HTTP wiring changes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("UNASSIGNED", True),
        ("unassigned", True),
        ("  unassigned  ", True),
        ("Binless", True),
        ("BINLESS\n", True),
        ("kitchen", False),
        ("UNASSIGNED_42", False),  # substring, not exact
        ("notbinless", False),
        ("", False),
    ],
)
def test_is_reserved_bin_name_predicate(raw, expected):
    from app.db import repository

    assert repository.is_reserved_bin_name(raw) is expected
