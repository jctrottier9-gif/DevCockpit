# DC-075B — Maintained releases and isolated hotfixes

Authority: [ADR-0017](architecture/ADR-0017-maintained-releases-and-forward-port.md),
[DC-075B / #135](https://github.com/tchi99/DevCockpit/issues/135),
and accepted [DeliveryContext V1](delivery-contexts.md).

## Activation is explicit

There is no automatic release creation from `main`, no implicit target
selection and no migration or deployment. A configured release or hotfix requires
an *accepted and persisted* immutable DeliveryContext, anchored to the exact
GitHub issue body. The delivery WorkItem must be executable in the valid,
current canonical roadmap. `RELEASE` is a manual preparation operation;
`HOTFIX` can enter the existing deterministic DEV loop after target verification.
`FORWARD_PORT` remains disabled pending DC-075C.

The consuming repository must have its own `AGENTS.md` aligned, independently
authorized, before its first orchestrated hotfix. Required release-branch
protections (strict named checks and required approval) must be in place **on
the release**, not merely on `main`. An inaccessible/unprotected release
blocks automatic operations; GitHub review and merge restrictions are not
bypassed.

## Operations

With an accepted contract already persisted, the API exposes:

- `POST /api/projects/{project_id}/deliveries/{work_item_id}/prepare`:
  idempotently creates/reuses a release branch at the accepted origin or
  creates/reuses an exact hotfix branch at the verified release tip.
  Existing release branches must prove origin ancestry; historic tag/source
  SHA does not silently change when a maintained release advances.
- `POST /api/projects/{project_id}/deliveries/{work_item_id}/hotfix-pr`:
  send `{"title":"FIX-42 — correction", "expected_head_sha":"<full-40-character-sha>", "description":"..."}`
  to create or rediscover exactly one PR from the accepted hotfix branch
  toward the accepted `release/x.y`. The PR body records `Work-Item`,
  `Delivery-Context-SHA256`, `Correction-Id` and `Target-Release`.
  A duplicate, retarget, mismatched SHA or non-descendant branch blocks.
- `GET /api/projects/{project_id}/deliveries/{work_item_id}/artifact` with
  `tag`, `integrated_sha`, `validation_check` query parameters checks
  a separately published GitHub release and corrective tag, linked source
  SHA, an `image@sha256:<immutable-digest>` reference, and required green
  target-specific validation checks.

The normal PR projection, same-session CI-red/watchdog recovery and
ResourceLock workflow continue to operate. Hotfix PR identity uses the exact
head branch, GitHub repository ID, base **name**, immutable contract
fingerprint and WorkItem. The branch comparison is against the release branch,
not the repository default. The finalizer revalidates GitHub target/head,
accepted source/issue, expected current base SHA, branch protection and named
required successful checks before synchronization or merge. Any ambiguous
write requires rediscovery instead of an unguarded retry. Hotfix operations
also acquire an exclusive per-repository/per-release lock.

## Publication is separate from merge

The consumer's existing CI/publisher, or an explicitly authorized separate
publisher, must build from the **actually integrated release SHA** and publish
the corrective tag plus image. DevCockpit DC-075B does not create a Docker
image, sign a registry artifact or deploy. A GitHub release needs evidence in
its body, in exact individual lines:

```text
Source-SHA: <integrated-40-character-commit-sha>
Docker-Image: registry.example/app@sha256:<64-lowercase-hex-digest>
Validation-Check: <required-release-protection-check-name>
```

DevCockpit checks the recorded digest syntax and source/tag GitHub evidence;
**it does not independently query a container registry**. The image digest
must first be verified and published by a trusted consuming CI/publisher.
A GitHub release body's arbitrary prose is not a registry attestation.

A merged HOTFIX with green current-head PR CI stays `MERGED` rather than
`ROADMAP_UPDATE_REQUIRED` until the corrective tag, integrated SHA,
published image digest and required validation evidence are present. This
applies independently of any future forward-port. A failed or missing
publication must be resumed without recreating the hotfix PR. Deployment
evidence is distinct and is **never** inferred from publication.

Schema compatibility is not inferred from tags or Docker layers. Missing
proof about SQL Server backward/forward compatibility, destructive
migrations, backup/restore or old-app-after-migration behavior is a manual
release blocker. No automatic migration, schema rollback or image deployment.

## Safety boundaries

- The accepted issue body and versioned contract remain immutable; a source
  tag move, missing origin, source/base mismatch or different PR target blocks.
- Synchronize a hotfix only with `release/x.y`. Never merge `main` into
  release or entire release into `main`.
- No empty CI result, skipped required check or stale head is green.
- Branch creation is idempotent; after an ambiguous PR-creation error,
  rediscover before trying again.
- **Forward-port is out of scope** until DC-075C, including cherry-picks,
  commit remapping and its separate PR lifecycle.
