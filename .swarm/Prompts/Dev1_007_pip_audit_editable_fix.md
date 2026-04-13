# Dev1_007 — Fix CI pip-audit failure (editable project in exported requirements)

Small CI fix. One file: `.github/workflows/ci.yml`.

## Background

The `pip-audit` job (CI run 71003844314) fails with:

```
ERROR:pip_audit._virtual_env:internal pip failure: ERROR: The editable
requirement file:///home/runner/work/binbrain/binbrain (from -r /tmp/requirements.txt
line 3) cannot be installed when requiring hashes, because there is no
single file to hash.
```

## Root Cause

`.github/workflows/ci.yml:78` runs:

```yaml
uv export --frozen --no-dev --format requirements-txt -o /tmp/requirements.txt
```

`uv export` emits the workspace project itself as an editable `-e file:///...` entry (line 3 of the generated requirements.txt). `pip-audit --strict` pins this to hash-checking mode, and pip refuses to install editable requirements when hashes are required.

We don't want to audit our own source — only third-party dependencies.

## Fix

Add `--no-emit-project` to the `uv export` invocation so the workspace package is omitted from the requirements file. Also consider `--no-emit-workspace` if any workspace members exist (check `api/pyproject.toml`).

Proposed replacement for ci.yml:78:

```yaml
run: uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt
```

Verify locally first:

```bash
cd api
uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt
head -5 /tmp/requirements.txt   # confirm no 'file:///' line
uvx pip-audit --requirement /tmp/requirements.txt --strict
```

Expect pip-audit to succeed (or report only the known `fastembed`/Pillow residual CVE — that's still covered by `continue-on-error: true` on ci.yml:63).

## Constraints

- Do NOT remove `continue-on-error: true` — the fastembed pin is load-bearing (see project memory). Only fix the editable-install error so pip-audit can actually run and report real CVEs.
- Do NOT touch `pyproject.toml` or `uv.lock`.
- One-line workflow edit + local verification is sufficient. No new tests needed.

## Deliverable

- Branch: `dev/Developer1` (your existing worktree)
- Commit the ci.yml change
- Open PR targeting `main` referencing CI job 71003844314
- Mention that SECURITY should re-verify the pip-audit job runs to completion on the PR
