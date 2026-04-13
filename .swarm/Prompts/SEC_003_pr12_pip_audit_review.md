# SEC_003 — Review PR #12 (pip-audit CI fix)

PR: https://github.com/stephenfeather/binbrain/pull/12
Branch: `dev/Developer1`
Change scope: one line in `.github/workflows/ci.yml` — added `--no-emit-project` to `uv export`.

## What to verify

1. **Change is minimal and correct.** Confirm the diff only touches ci.yml:78 (the `uv export` line) — no other files, no pyproject/uv.lock edits.
2. **Requirements file no longer contains editable project.** On the PR checkout, run:
   ```bash
   cd api
   uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt
   head -10 /tmp/requirements.txt
   ```
   Confirm no `-e file:///...` or `file:///...binbrain` line appears.
3. **pip-audit actually runs to completion now.** Either let the PR CI run and confirm the `pip-audit` job reaches the audit step (not just the install error), or reproduce locally:
   ```bash
   uvx pip-audit --requirement /tmp/requirements.txt --strict
   ```
   Expected outcome: either clean or the single known `pillow 10.4.0 / CVE-2026-25990` finding (FF-02, already tracked).
4. **No regression in dependency coverage.** `--no-emit-project` omits only the workspace package itself. Confirm all third-party deps (fastembed, Pillow, onnx, starlette, python-multipart, requests, etc.) still appear in the exported requirements.
5. **`continue-on-error: true` still present** on the pip-audit job. Do NOT remove it — the fastembed pin is load-bearing (FF-02).

## Out of scope

- FF-02 Pillow upgrade (fastembed pin blocker, tracked separately).
- FF-03 / FF-04 (Developer2 is working FF-03 in parallel; FF-04 untouched).

## Deliverable

Post review verdict as a PR comment on #12: APPROVE / REQUEST CHANGES with evidence. Also write a short assessment to `.swarm/AssessmentReports/SECURITY_PR12_REVIEW_120426.md` summarizing the checks above and the result.

If APPROVED, notify Architect (pane binbrain:0.0 or via Prompt file) so the merge can proceed.
