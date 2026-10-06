---
status: accepted
work_item: system-deploy-ops-defects
---

## Why

Trust Root system deployment could leave Monitor without usable GitHub provider data, reject claims between refreshes, omit quota shadow receipts, read project configuration under an inaccessible operator home, miss newly added work items after upgrade, and repeatedly dispatch legacy automatic slice specs after adoption.

## Goals

- Keep the Manager source checkout synchronized while giving Monitor read-only access.
- Keep provider freshness, quota shadow admission, model identities, and workspace resolution usable without operator edits after installation or upgrade.
- Park legacy automatic slice specs once, reversibly, before they can enter fanout.
- Document pre-archive behavior and verification.

## What Changes

- Move source checkout synchronization to Manager and use a read-only Monitor provider.
- Validate Monitor refresh and claim freshness intervals against the shared maximum age.
- Provide a system-only default quota shadow context and a `PSC_REPO_ROOT` workspace override.
- Include supported identities in the packaged roster and provide the Manager-owned identity overlay command.
- Park existing top-level automatic slice specs once on adoption/upgrade.
- Add regression coverage and update the Trust Root runbook and changelog.

## Scope

This change records implementation and verification before archive. It does not claim archive, merge, issue closure, or delivery completion.
