---
status: accepted
work_item: system-deploy-ops-defects
---

## Context

The system Monitor unit is intentionally isolated from operator home directories and must not write the Manager-owned source checkout. The Manager can write that checkout and already owns periodic work orchestration. Claim freshness is bounded at 900 seconds, so Monitor refresh policy must remain below that limit in both deployment modes.

## Decisions

1. Manager fetches GitHub's advertised default branch into a private ref with `--no-write-fetch-head`; it fast-forwards only a clean tracked checkout. Dirty, ahead, or diverged checkouts remain untouched.
2. Monitor's system unit disables Git fetch and resolves its project workspace from `PSC_REPO_ROOT`. Missing checkout objects leave the provider degraded until Manager synchronization succeeds.
3. Monitor configuration rejects stale thresholds above 900 seconds and refresh intervals that are not below the effective stale threshold.
4. The system Manager creates an in-memory unknown-capacity quota configuration only when the operator has no quota file. It records shadow decisions and does not enable enforcement.
5. Existing automatic slice specs are moved to a dated hidden directory once, with their bytes preserved and a durable marker preventing later specs from being parked.

## Verification

Regression tests cover read-only provider access, interval validation, system workspace and unit settings, default shadow admission, Manager fast-forward synchronization, and reversible one-time parking. The runbook describes operator checks and recovery.

## Delivery Boundary

These decisions cover pre-archive implementation only. Archive, merge, issue closure, and final completion are performed by the delivery manager.
