"""CI guard: if the VLM prompt text changes, ``PROMPT_VERSION`` and
``PROMPT_VERSION_HASH`` MUST both be reviewed in the same commit. This protects
the analytics lineage (``photo_detections.prompt_version``, ``/suggest``
responses, ``photo_suggestion_outcomes.prompt_version``) from silent version
drift — see Q-prompt-version closeout (ApiDev_009).
"""

import hashlib

from app.services import vision


def test_prompt_version_hash_matches_prompt_text() -> None:
    current_hash = hashlib.sha256(vision._PROMPT.encode("utf-8")).hexdigest()[:8]
    assert vision.PROMPT_VERSION_HASH == current_hash, (
        "\n"
        f"_PROMPT was edited but PROMPT_VERSION_HASH is still pinned to "
        f"{vision.PROMPT_VERSION_HASH!r}.\n"
        f"  Current prompt hashes to: {current_hash!r}\n"
        f"  Pinned PROMPT_VERSION_HASH: {vision.PROMPT_VERSION_HASH!r}\n"
        f"  Current PROMPT_VERSION:    {vision.PROMPT_VERSION!r}\n"
        "\n"
        "To fix:\n"
        f"  - Semantic change:  bump PROMPT_VERSION (e.g. 'v2' -> 'v3') "
        f"AND set PROMPT_VERSION_HASH = {current_hash!r}.\n"
        f"  - Whitespace/polish: set PROMPT_VERSION_HASH = {current_hash!r} "
        "and leave PROMPT_VERSION unchanged — but think twice, because "
        "analytics will not distinguish old-prompt rows from new-prompt rows."
    )
