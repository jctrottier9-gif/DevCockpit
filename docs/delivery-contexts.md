# DC-075A — Accepted delivery context V1

Authority: [ADR-0017](architecture/ADR-0017-maintained-releases-and-forward-port.md),
[WorkItem #134](https://github.com/tchi99/DevCockpit/issues/134),
and the canonical V3 roadmap in [#1](https://github.com/tchi99/DevCockpit/issues/1).

## Operational boundary

This tranche **does not enable** maintained release creation, hotfix PR creation,
forward-port cherry-picks, publication or deployment. Only `NORMAL` remains
executable by the existing scheduler/finalizer. If `delivery_contexts` includes
a `RELEASE`, `HOTFIX` or `FORWARD_PORT` context, evaluation fails closed with
`RELEASE_AUTOMATION_DISABLED`. DC-075B must explicitly add target-specific
GitHub association, finalization, protection/CI checks, publication and
recovery before enabling the corresponding path.

For legacy configured projects with no delivery context, `NORMAL` still
resolves **current real main**, and existing AgentSession, wire protocol and
idempotency keys are unchanged. An **explicit** NORMAL context is verified
against GitHub; if its recorded base/ref has moved, do not reuse it silently.
Missing/unknown mode in an explicit structured contract fails closed.
A missing contract must never be interpreted as HOTFIX.

## Accepted Git source/target snapshot

`DeliveryContext` schema_version=1 is an immutable, per-WorkItem record:

| Category | Immutable fields |
| --- | --- |
| Authority | `repository_id`, `repository_full_name`, `work_item_id`, `delivery_issue_number`, `accepted_issue_body_sha256` |
| Mode/source | `mode`, `requested_ref`, `ref_kind`, `resolved_ref`, `source_sha` |
| Working target | `expected_work_branch`, `starting_sha`, `expected_pr_base`, `observed_pr_base_sha` |
| Release | `release_id`, `release_branch`, `release_origin_sha`, `release_state` |
| Forward-port | `correction_id`, `linked_work_item`, `source_hotfix_pr`, `integrated_commits`, `forward_port_method` |
| Completion | `completion_policy` |

Source kind is exactly `branch`, `tag`, or **full** 40-character commit `sha`.
Use qualified `refs/heads/*` or `refs/tags/*` where applicable. Resolve
annotated tags all the way to commits. A tag's accepted commit never changes;
the maintained branch tip may advance only through independently verified
hotfix deliveries. A changed tip is a **new GitHub observation**, not a new
historical release origin.

For an approved correction, declare **two distinct V3 WorkItems** up front:
release hotfix and forward-port, linked by functional correction identity.
Each has a separate PR base, branch, agent session, CI, merge and reconciliation.
The forward-port targets current main, but sources the **actually integrated**
fix from the release, not the original pre-squash head and not the whole
release branch.

## Persistence and authority

`delivery_contexts` is an additive Alembic 0011 table. It persists the
canonical serialized contract, fingerprint, repository ID, WorkItem, issue
number, issue-body hash and acceptance timestamp. Exact repeated writes are
idempotent; contradictory changes to accepted snapshots are rejected. The
repository `add_accepted` API presumes its caller has obtained an **explicit
human/issue acceptance**; it is *not* itself an approval API. No mutation route
to accept/replace contracts is exposed in DC-075A.

Optional `delivery_contexts` configuration is a list of full structured
contracts inside each `projects.json` Project. This config carries a
candidate/pinned snapshot, not authority for release operations. The
GitHub reference reader independently checks the actual repository ID and
owner/name, issue-body fingerprint, qualified tag/branch/commit, current base
tip and, when there is a PR, the exact PR head repository and base name.
It fails closed on absent/moved refs, missing branches, ambiguous tag targets,
stale base SHA or PR retargeting even when different branches share a tip.
The historical starting SHA and new observations must remain distinguishable.

## Completion policy

`CODE_MERGED` requires a merged PR and current-head green CI.
`VERIFIED_ARTIFACT` additionally requires immutable published tag,
source SHA, image digest and explicit validation proof. Hotfix/release modes
require `VERIFIED_ARTIFACT` and must **never** reconcile DONE on merge alone.
Publication and validation do not imply deployment; deployment and SQL
schema rollback are never automatic.

## Agent handoff

All initial/CI-red/stale/watchdog/conflict/reconciliation DEV prompts carry
revalidation instructions. Explicit contracts include repository, accepted
issue, source, target, version and fingerprint; agents must compare against
live GitHub **before editing**, on every resumption, and before PR/reconciliation.
Unverified migration or image compatibility is a blocker, not a reason
to modify schema or roll back automatically.

The consumer repository's own `AGENTS.md` (e.g. RessourcePlanner) must be
aligned in an independently authorized delivery before its first hotfix.
