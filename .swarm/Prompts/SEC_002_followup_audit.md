# SEC_002 — Follow-up Security Assessment

## Objective
Conduct a follow-up security assessment of the BinBrain repository. Verify remediations landed since SEC_001 (report at `.swarm/AssessmentReports/SECURITY_ASSESSMENT_120426.md`) and re-sweep for new or missed vulnerabilities.

## Setup
1. Rebase your worktree onto `origin/main` so you are reviewing the current tip.
2. Create a new branch: `dev/SECURITY-002`.
3. Read the prior assessment at `.swarm/AssessmentReports/SECURITY_ASSESSMENT_120426.md` for context.

## Scope
- Full repository, focusing on changes landed since SEC_001. Relevant recent merges (all on `main`):
  - PR #6 — GitHub Actions CI (pytest + pip-audit + gitleaks)
  - PR #7 — test fixes (rate-limiter closure + schema path)
  - PR #8 — env-tunable tracked-keys cap + FIFO docstring (addresses rate-limiter polish, Issue #5)
  - PR #9 — HMAC-SHA256 + `METADATA_HASH_PEPPER` pepper for sensitive-field hashing (Issue #3)
  - PR #10 — per-IP rate-limit fallback + middleware-ordering startup assertion (Issue #4)

## Tasks
1. **Re-verify prior findings** (F-01 through F-11). For each:
   - FIXED, PARTIAL, STILL_OPEN, or REGRESSION.
   - Cite the remediating commit/PR and file:line for FIXED items.
   - For PARTIAL/STILL_OPEN, state the residual exposure.
2. **Fresh sweep** — full OWASP-Top-10 style review, with emphasis on:
   - HMAC pepper handling: fallback behavior when `METADATA_HASH_PEPPER` is unset (empty-key HMAC), secret sourcing, rotation implications.
   - Middleware ordering invariant: correctness of `_assert_auth_runs_before_rate_limit` under edge cases (decorator ordering, reload, missing middleware).
   - Per-IP rate-limit fallback: spoofable client IPs, proxy/XFF handling, bucket exhaustion from spoofed IPs.
   - CI workflow: secrets exposure, pinning, third-party action trust, gitleaks/pip-audit coverage.
   - Any new routes, dependencies, or configuration surfaces introduced since SEC_001.
3. **Dependency audit** — re-run `pip-audit` on the current lockfile; call out new CVEs and any still-unresolved findings from SEC_001 (F-06).
4. **Secrets scan** — re-run gitleaks across the full history.

## Output
Commit an assessment report to `.swarm/AssessmentReports/SECURITY_ASSESSMENT_002_<MMDDYY>.md` following the SEC_001 format:
- Executive summary
- Per-finding disposition table for F-01..F-11 (FIXED/PARTIAL/STILL_OPEN/REGRESSION + evidence)
- New findings table (IDs `F-12`, `F-13`, ...) with Severity, Probability, Evidence
- Detailed findings section with remediation guidance
- Threat model delta since SEC_001

Commit and push to `dev/SECURITY-002`. Do **not** open a PR — the Architect will review the report directly.

## Completion
When done, output exactly:
`ARCHITECT TASK COMPLETED: security follow-up assessment committed to dev/SECURITY-002 at .swarm/AssessmentReports/SECURITY_ASSESSMENT_002_<MMDDYY>.md`
