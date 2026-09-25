## ADDED Requirements

### Requirement: Target discovery timeout is distinctly classified and retried once

The pinned Archify visual-check MUST recognize only a 15000 ms timeout from Target.getTargets as a recoverable startup condition. It MUST record the method, timeout, elapsed duration, attempt, Archify revision, Chrome identity/process state, and bounded stderr evidence, then MAY retry that read-only request exactly once after one second on the same live browser process and CDP pipe. It MUST NOT retry other failures.

#### Scenario: Target discovery responds on the bounded retry

- **WHEN** the first Target.getTargets request times out and the second request returns a page target
- **THEN** visual-check records the first timeout and recovery and continues through every existing visual inspection and capture

#### Scenario: Target discovery remains unresponsive

- **WHEN** both Target.getTargets requests time out
- **THEN** visual-check exits nonzero with both attempt records and Chrome process evidence

#### Scenario: A different request or visual check fails

- **WHEN** another CDP method times out, Chrome exits, a pipe fails, or a visual assertion fails
- **THEN** the error remains a nonzero failure and no target-discovery retry is attempted

### Requirement: Architecture HTML CI preserves diagnostics without weakening acceptance

The Architecture HTML workflow MUST keep its exact HTML comparison, required Archify inspection, native Playwright navigation/interaction review, and capture evidence. It MUST upload runner context, Archify revision, Chrome/Node identities, visual-check JSON, logs, and available captures even when a required step fails. It MUST propagate a failed visual-check exit status.

#### Scenario: Recovered startup still completes all acceptance checks

- **WHEN** the retry succeeds
- **THEN** the native browser review and all existing visual/capture checks still run and the final status reflects their results

#### Scenario: Startup retry is exhausted

- **WHEN** the second target-discovery request times out
- **THEN** the Architecture HTML check remains failed and the always-uploaded artifact contains the failed receipt and diagnostics
