# DC-075C — Controlled forward-port of an integrated hotfix

Authority: [ADR-0017](architecture/ADR-0017-maintained-releases-and-forward-port.md),
[DC-075C / #136](https://github.com/tchi99/DevCockpit/issues/136), and
the immutable [DeliveryContext V1](delivery-contexts.md).

A forward-port is a **second predeclared WorkItem**, linked to a previously
delivered HOTFIX WorkItem and its merged GitHub PR. It is not evidence that
the hotfix was published, deployed or that main was corrected just because
the maintained release contains the fix.

## Safe, explicit operations

The backend accepts the requested operation only when **both** delivery
contracts match their exact persisted acceptance, the canonical roadmap is
valid, the hotfix dependency is DONE, and the forward-port is READY.

- `POST /api/projects/{project_id}/deliveries/{work_item_id}/forward-port/prepare`
  re-reads repository ID, issue body fingerprints, both refs, release ancestry,
  actual integrated GitHub PR and commits, and the accepted main tip. It
  creates/reuses only the separate forward-port branch at the proven main SHA.
- The response contains the exact integrated SHA(s), method and an action.
  **The DEV** then applies the fix with `git cherry-pick -x <integrated-SHA>`
  for the squash or integrated linear commits. For a merge commit, derive and
  inspect the *release-relative first-parent delta*, including merge
  resolutions, and apply a separately reviewed adaptation. Do **not**
  cherry-pick an arbitrary merge mainline or merge the whole release into main.
  Conflicts and partial presence need an explicit same-session DEV resolution.
- `POST /api/projects/{project_id}/deliveries/{work_item_id}/forward-port/pr`
  takes `{"title":"FWD-1 — forward-port fix","expected_head_sha":"<40-hex-sha>","description":"..."}`.
  It requires a nonempty branch diff ahead of current main, complete GitHub
  commit evidence and a provenance footer for **every** source commit:
  `(cherry picked from commit <40-hex-sha>)`, or the two explicit lines
  `Forward-Port-Of: <40-hex-sha>` and `Forward-Port-Reason: ...` for a
  documented conflict adaptation. It checks strict required CI + PR review
  protection on **main**, searches all PR pages, and reuses only an exactly
  matching PR to main.

The PR body retains: `Work-Item`, `Delivery-Context-SHA256`,
`Correction-Id`, `Source-Hotfix-WorkItem`, `Source-Hotfix-PR`,
`Integrated-Commits`, `Forward-Port-Method` and `Target-Branch: main`.
GitHub's separate current-head CI, review and merge gate continue through
the existing execution projection/finalizer; roadmap reconciliation occurs
only after this second PR is merged and green.

## Fail-closed cases

A stale or deleted main/release ref, moved acceptance issue, repository/PR
identity mismatch, wrong target base, missing integrated commit, unknown
merge parent, incomplete pagination, duplicate PR, wrong branch lineage,
empty or partially applied patch, missing provenance, unprotected target,
CI/review not required, or a GitHub API timeout stops automatic mutation.
Recover by re-reading GitHub; never blindly retry an uncertain creation.

A fix already on main requires explicit patch-equivalence evidence and a
human decision; the workflow does not forge an empty cherry-pick commit or
mark the forward-port DONE automatically. Main drift after accepted source
selection requires a fresh accepted target, not an unapproved rebase. A
cherry-pick conflict is a DEV issue to resolve and test; a reviewed PR
cannot be replaced by a direct push or force merge.

## Boundaries

DC-075C does not add UI, Docker deployment, migration execution, artifact
publication, automatic patch application, or hidden consumer repository
changes; these are separate responsibilities. In particular, the system
**prepares and verifies** the GitHub branch/PR; the DEV applies the
integration-preserving cherry-pick or adaptation and provides code/tests.
The source hotfix and independent forward-port each retain their own
AgentSession, CI and eventual roadmap reconciliation. The consumer must
separately prove SQL Server schema backward/forward compatibility and
backup/rollback requirements before deploying.
