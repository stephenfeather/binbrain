# Dev1_005 — Close GH Issue #3: HMAC+pepper for sensitive-field hashing

Minor defense-in-depth follow-up from PR #2 aegis re-review. One file: `api/app/services/metadata_schema.py`.

## Background

`_hash_sensitive_fields` currently uses plain `hashlib.sha256(v.encode()).hexdigest()` on device-identity values (device_id, IMEI, MAC, serial). SHA-256 is deterministic and trivially rainbow-tabled for known identifier spaces (IMEI ~10^15, MAC 2^48). Swap it for HMAC-SHA256 with a server-side pepper from env so hashes aren't correlatable across installs and aren't brute-forceable for a specific device.

## Scope

### 1. Add pepper-aware hashing

In `api/app/services/metadata_schema.py`:

```python
import hmac
import os

def _get_pepper() -> bytes:
    """Read METADATA_HASH_PEPPER from env each call (test-friendly)."""
    return os.environ.get("METADATA_HASH_PEPPER", "").encode("utf-8")


def _hash_value(value: str) -> str:
    return hmac.new(_get_pepper(), value.encode("utf-8"), hashlib.sha256).hexdigest()
```

Replace the inline `hashlib.sha256(v.encode("utf-8")).hexdigest()` call in `_hash_sensitive_fields` (line 96) with `_hash_value(v)`.

### 2. Update module docstring

Change the "Sensitive fields" constraint description and the inline comment at line 43 from "SHA-256 hashed" to "HMAC-SHA256 hashed with server-side pepper (env `METADATA_HASH_PEPPER`)". Note that when the pepper is unset the HMAC key is empty — still deterministic but not cross-install-correlatable once set.

### 3. Update tests

`api/tests/test_security_f11_metadata_schema.py` has 4 assertions that compute `hashlib.sha256(raw.encode()).hexdigest()` as the expected value. Update each to use HMAC with the current pepper:

```python
import hmac, os
expected = hmac.new(
    os.environ.get("METADATA_HASH_PEPPER", "").encode("utf-8"),
    raw.encode("utf-8"),
    hashlib.sha256,
).hexdigest()
```

Affected tests:
- `test_sensitive_field_device_id_is_hashed`
- `test_sensitive_field_imei_is_hashed`
- `test_sensitive_field_mac_is_hashed`
- `test_ingest_device_id_stored_hashed`

### 4. Add new tests

Add two tests to the same file:

1. **`test_pepper_changes_hash_output`** — set `METADATA_HASH_PEPPER=pepper-a` via `monkeypatch.setenv`, hash a value, then set `METADATA_HASH_PEPPER=pepper-b`, hash the same value, assert the outputs differ and neither equals the plain SHA-256.
2. **`test_no_pepper_is_deterministic`** — with `monkeypatch.delenv("METADATA_HASH_PEPPER", raising=False)`, hash the same value twice, assert equal and non-empty.

Use `monkeypatch` fixture from pytest.

## Success criteria

1. `_hash_sensitive_fields` uses HMAC-SHA256 with the pepper from `METADATA_HASH_PEPPER`.
2. Module docstring and the inline comment about sensitive fields mention HMAC and the pepper env var.
3. All existing F-11 tests pass after being updated to compute HMAC expected values.
4. The two new tests (pepper changes output, no pepper is deterministic) pass.
5. Full suite green: `TEST_DATABASE_URL=postgresql+psycopg://binbrain:claude_dev@localhost:5434/binbrain uv run --project api --python 3.12 pytest api/tests -q` → 255 passed (253 existing + 2 new).

## Process

- Branch: `fix/issue-3-hmac-pepper` off `origin/main`
- TDD: update/add tests first, watch them fail, then implement.
- One commit is fine — this is small and focused.
- PR body must include `Closes #3`.
- Note in the commit body that this is a fresh deployment so no existing device-identity hashes need rotation.
- Say `ARCHITECT TASK COMPLETED: Pull Request #N` when done.

## Notes / non-goals

- Do NOT add pepper validation (e.g., "require non-empty in prod"). That's a separate concern; for now the env var is optional and absence degrades gracefully to empty-key HMAC.
- Do NOT touch any other files. If you find yourself editing something outside `metadata_schema.py` or `test_security_f11_metadata_schema.py`, stop and ARCHITECT REQUEST.
