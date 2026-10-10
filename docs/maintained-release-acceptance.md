# DC-075D — Cockpit and acceptance for maintained releases

Authority: [ADR-0017](architecture/ADR-0017-maintained-releases-and-forward-port.md),
[DC-075D / #137](https://github.com/tchi99/DevCockpit/issues/137),
the accepted [DeliveryContext V1](delivery-contexts.md),
the [release workflow](release-workflow.md) and the [forward-port workflow](forward-port.md).

## Operating the cockpit

1. Select the consuming **project** in the existing workspace selector. Open
   **Releases · hotfix · forward-port** and filter by maintained release, exact
   source tag/branch/full SHA, or WorkItem. The selector is navigation only,
   **not acceptance**: acceptance remains the human-authorized, persisted
   DeliveryContext bound to the delivery issue and its fingerprint.
2. Confirm the frozen origin and full source SHA, current release branch,
   accepted working branch and **expected PR base name**. GitHub may move
   the maintained release tip after a validated fix, but the originating tag
   and immutable acceptance SHA must not be rewritten.
3. Inspect the HOTFIX and FORWARD_PORT **separately**: two WorkItems, two
   AgentSessions, two PRs, two current-head CI results and merge events.
   The hotfix PR must target `release/x.y`; the forward-port PR must target
   `main`. Click a PR or WorkItem to continue in Reviewer/Orchestration.
   Reviewer, DEV Pool and Attention Center link to the release supervision.
4. Check corrective version/tag, integrated source SHA, digest and validation
   check before treating the release as published. Publication does not imply
   deployment. A GitHub release and a syntactically valid image digest are
   **not independent registry attestation**. Verify the immutable registry
   digest through the trusted consuming CI/publisher before release approval.
5. Validate SQL Server migrations and deployment independently. The cockpit
   deliberately reports `NOT_ATTESTED` for SQL compatibility and
   `NOT_VERIFIED` for deployment; no current DeliveryContext field or
   GitHub-only projection can prove them. Never interpret these statuses as
   safe-to-deploy decisions. Do not perform schema migration or rollback from
   the cockpit. If external evidence reports an incompatible migration, stop
   the release and require an explicit architecture/operations decision.

A panel can show `BLOCKED` if the canonical roadmap, persistence, linked
contracts, source/target PR identity, GitHub evidence or publication checks
are incomplete. There is no fallback to `main` for hotfixes and no inferred
success from a missing CI run. It is read-only and reconstructs its evidence
from accepted configuration/persistence and GitHub after restart.

## Align each consuming repository's AGENTS.md — separate delivery

Before the **first** orchestrated hotfix for RessourcePlanner (or any other
consumer), authorize a separate change in **that** repository. It must tell
DEV agents to:

- Read the accepted WorkItem DeliveryContext, its GitHub delivery issue and
  live ref evidence before changing code. For NORMAL, verify current main;
  for RELEASE/HOTFIX, start only from the accepted release SHA/branch; for
  FORWARD_PORT, start from independently verified current main and carry
  only the proven integrated release fix.
- Require exact repository ID/name, 40-character SHA, immutable tag origin,
  target branch name and head, PR fingerprint, protection rules and CI;
  stop if any GitHub evidence moved, disappeared or contradicts acceptance.
- Never merge main into the maintained release or the entire release into
  main; preserve source provenance with `cherry-pick -x` or reviewed delta
  adaptation for merge resolutions, and stop on conflicts.
- Keep hotfix and forward-port sessions/PRs/reconciliation distinct.
  Require release-specific branch protections, review and named strict CI,
  publication/validation evidence and an independent registry digest.
- Analyze SQL Server forwards/backwards schema support, destructive changes,
  old-app-after-new-schema behavior, backup and restore rehearsal. Never
  automatically deploy, migrate or roll back schema on an agent prompt.

The DevCockpit PR must **not** edit another repository's `AGENTS.md`.
Track consumer adoption as its own authorized issue/PR.

## End-to-end acceptance matrix

Use an isolated, nonproduction consuming repository or fixture environment
with real protected `release/1.4` and `main` branches, mandatory CI/review,
and a test Docker registry. Do not reuse production credentials or databases.

| Case | Required observation |
| --- | --- |
| Version N / main advances | Tag resolves to the same full origin SHA; maintained branch does **not** inherit new main features |
| Hotfix N.0.1 | Work branch starts at verified release tip; PR #1 base is `release/1.4`; required release checks and review are observed |
| Merge without publish | Code merge is observable, artifact is PENDING, WorkItem is not reconciled DONE without required image evidence |
| Publish + validate | Corrective tag, integrated SHA, immutable image digest and release check match; independent publisher confirms actual registry digest; deployment remains unverified |
| Forward-port | A second WorkItem uses proven integrated commit(s), fresh main, second PR and CI; only the patch is carried, not release-only history |
| Wrong base / identical SHA | Retargeted PR is BLOCKED by **base name**, even if main and release base SHAs temporarily match |
| Moved tag / stale accepted base | No branch/PR creation or merge; visible fail-closed diagnostics |
| Empty, failed, partial or stale CI | No implied green or authorized automatic merge; Reviewer retains actual run/head evidence |
| Timeout and restart | No duplicate WorkItem, branch or PR; panel rehydrates independently from persisted contracts and GitHub |
| Squash / merge / conflict | Integrated commit or reviewed release-relative delta traced; a conflict returns to same DEV session and is not treated as DONE |
| Missing / incompatible SQL migration | Never infer compatibility from image/tag; block real deployment pending separately reviewed schema/backup/restore proof |
| ARCH gate / ambiguous companion send | Still human authorized/manual as specified; no extra automatic ARCH send or retry after AMBIGUOUS |

Automated regression tests in `tests/test_release_panel.py` cover the
read-only projection, two independent PRs/targets, CI red and merge states,
missing publication, wrong base, missing acceptance, unavailable evidence,
roadmap validity and restart reconstruction. Existing
`tests/test_release_workflow.py`, `tests/test_forward_port.py`,
`tests/test_github_execution_adapter.py` and companion tests cover the
underlying mutations and transport. **Real image deployment and SQL Server
compatibility must be exercised in the isolated consumer environment**;
mock tests cannot attest to those external operations.

## Validation commands

```bash
python -m compileall -q app tests
pytest -q tests/test_release_panel.py tests/test_release_workflow.py tests/test_forward_port.py
pytest -q
cd frontend && npm ci --no-audit --no-fund && npm run build
cd ../extension && npm ci --no-audit --no-fund && npm test
```
