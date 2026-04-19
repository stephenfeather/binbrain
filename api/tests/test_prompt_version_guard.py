"""CI guard: if the VLM prompt text changes, ``PROMPT_VERSION`` and
``PROMPT_VERSION_HASH`` MUST both be reviewed in the same commit. This protects
the analytics lineage (``photo_detections.prompt_version``, ``/suggest``
responses, ``photo_suggestion_outcomes.prompt_version``) from silent version
drift — see Q-prompt-version closeout (ApiDev_009).
"""

import hashlib

from app.services import vision


def test_prompt_version_hash_matches_prompt_text() -> None:
    current_hash = hashlib.sha256(vision._PROMPT.encode()).hexdigest()[:8]
    assert vision.PROMPT_VERSION_HASH == current_hash, (
        f"_PROMPT was edited but PROMPT_VERSION_HASH is still pinned to "
        f"{vision.PROMPT_VERSION_HASH!r}. The current prompt hashes to "
        f"{current_hash!r}. If this is a semantic change, bump PROMPT_VERSION "
        f"(e.g. 'v2' -> 'v3') AND set PROMPT_VERSION_HASH = {current_hash!r}. "
        f"If it's a whitespace / wording-polish change that should NOT count "
        f"as a new version, just update PROMPT_VERSION_HASH to {current_hash!r} "
        f"and leave PROMPT_VERSION unchanged — but think twice, because "
        f"analytics will not be able to distinguish old-prompt rows from "
        f"new-prompt rows."
    )
