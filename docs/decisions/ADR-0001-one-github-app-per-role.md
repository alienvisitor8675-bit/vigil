# ADR-0001: One GitHub App per role, deferred

- **Status:** accepted (deferral), 2026-10-22
- **Deciders:** the owner; recorded by the phase 1 worker
- **Layer:** 4 of the documentation standard (`docs/design.md`). This is
  the first ADR.

## Context

`docs/design.md` ("Frontier practices") wants one GitHub App per role:
vigil-coord, vigil-worker and vigil-reviewer. Each role's commits, comments
and reviews would then be attributed to it, with its own permissions.

Phase 1 has one App, **vigil-worker**. The coordinator verified it on
2026-10-22:
- `GET /app` answers with slug `vigil-worker`;
- there is one installation, on all eight of the owner's repos;
- it can mint and revoke tokens on vigil, parkagent and claude-mods.

Its permissions are contents:write, pull_requests:write, issues:write,
checks:read, metadata:read and artifact_metadata:read.

Two facts limit what one App can promise:
- A GitHub App has one permission set on every installation. So its
  installation on claude-mods could write, and so could its installations on
  private repos vigil doesn't work on.
- In phase 1, the reviewer doesn't exist yet. The coord and the workers
  never call GitHub themselves: vigild publishes from the host (P2).

## Options

1. **One App for every role, now.** vigild down-scopes each token:
   - one repo per token;
   - only repos listed in `configs/repos.yaml`, plus claude-mods read-only;
   - only the permissions the use needs.
2. **One App per role now.** This needs three Apps, three keys and three
   sets of installations before phase 1 can run, for attribution the owner
   doesn't read yet.
3. **A second, read-only App for claude-mods only.** This is the
   alternative in `docs/runbook.md`.

## Choice

**Option 1.** vigil-worker serves every role for now. The code holds the
lines that matter:
- `vigild/github/app.py` refuses any repo not in `configs/repos.yaml`. The
  exception is claude-mods, where tokens are always `{"contents": "read"}`.
- Each permission has a ceiling: contents and pull_requests up to write;
  metadata and issues at read. Nothing else is ever requested. A token
  GitHub grants with more than was asked is revoked.
- Workers and coords never hold a GitHub token (P2). Only vigild does, on
  the host, for the length of one publish.
- Tests: `tests/github/test_tokens.py`.

## Consequences

- Every commit and PR from the fleet is attributed to vigil-worker. The
  role is in the PR title (`[vigil <job>]`), the branch name and vigild's
  records, not in GitHub's author field.
- A leaked App key can do anything the App can on all eight repos. vigild's
  down-scoping doesn't help against a stolen key. The key stays outside
  every container mount.

## When this changes

Revisit when any of these happens, and write ADR-0002:
- **Phase 2's reviewer posts reviews.** A review by "vigil-worker" on a
  vigil-worker PR reads as self-review. The reviewer gets its own App
  (`vigil-reviewer`, pull_requests:write and contents:read) at the latest
  when it posts its first review.
- **Auto-merge earns a shape (phase 6).** The merge path must be attributable
  to a role that can't also write code.
- **A repo is added whose owner wants per-role audit trails.**
- **The App key is rotated after an incident.** That is the moment to split
  roles.
