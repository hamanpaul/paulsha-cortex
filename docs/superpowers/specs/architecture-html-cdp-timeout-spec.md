---
status: accepted
work_item: architecture-html-cdp-timeout
issue: 1084
---

# Architecture HTML Chrome CDP target timeout 規格（#1084）

## Authority and evidence

- Authority: [issue #1084](https://github.com/hamanpaul/paulsha-cortex/issues/1084).
- Planning base: origin/main at 574b4f513c013e58705f0587b3bfb7dfaec7b788, fetched 2026-09-25.
- No active OpenSpec change or committed plan for this exact target-discovery timeout was found. The 2026-09-23 refine handoff records an earlier Architecture HTML rerun that passed after a flaky failure; it has no diagnosis or fix contract and is historical context only.
- #1067 head 822b105adccc11950ce44e794ad95074cf8b8e1a: attempt 1 failed at 2026-09-25 05:57:11Z; attempt 2 succeeded at 05:59:09Z; PR #1067 was merged at 06:06:18Z. [Attempt 1](https://github.com/hamanpaul/paulsha-cortex/actions/runs/36100607645/attempts/1) · [attempt 2](https://github.com/hamanpaul/paulsha-cortex/actions/runs/36100607645/attempts/2).
- #1083 head 0a5b4a624604649a5bfe447695d82395179aa44e: attempt 1 failed at 06:11:20Z; attempt 2 passed the Architecture HTML review at 06:13:27Z. At 06:26:44Z, all PR checks, including pytest 3.13, had passed and the PR remained open. [Attempt 1](https://github.com/hamanpaul/paulsha-cortex/actions/runs/36101642910/attempts/1) · [attempt 2](https://github.com/hamanpaul/paulsha-cortex/actions/runs/36101642910/attempts/2).
- The issue reports both failures at the same workflow step, with the same checked-in HTML, and a 15-second Target.getTargets timeout. Rerun success is evidence of intermittency; it does not establish whether runner pressure, Chrome startup, or Archify caused it, and a GitHub rerun does not reuse the same physical hosted runner.
- The workflow pins tt-a1i/archify at a07fa1d5b2a10cbea110c5a2be2817397a301cdc. Its [visual-check source](https://raw.githubusercontent.com/tt-a1i/archify/a07fa1d5b2a10cbea110c5a2be2817397a301cdc/archify/bin/visual-check.mjs) starts a pipe-based Chrome process, calls Target.getTargets during attach, and gives CDP requests a 15000 ms default. The timeout path rejects a plain error; the process status and stderr details are added for pipe/process failures, not for this timer rejection. The runtime catch writes viewer/visual-check-runtime and then closes the browser.
- Existing files are .github/workflows/architecture-html.yml and tests/architecture_browser_review.py. The workflow uploads /tmp/architecture-review on failure, but the current receipt lacks the Chrome details needed to separate the runner, browser, and tool layers.

## Boundary

This issue owns the Architecture HTML CI reliability contract and the integration with the pinned Archify engine. The upstream engine owns CDP request classification and browser-process diagnostics; this repository owns the pin, GitHub runner context, log/artifact retention, and the required native browser review. The current change is planning only.

The packet uses #1067 and #1083 run records as recurrence evidence. It does not change or act on the active #966 or #987 work, their source, runs, pull requests, or journals. It does not change architecture.html or its source data.

## Requirements

### R1. Target-discovery timeout has a distinct structured diagnostic

The pinned Archify visual-check SHALL classify a 15000 ms timeout from Target.getTargets as a target-discovery startup condition. The receipt SHALL record the CDP method, configured timeout, measured elapsed time, attempt number, Archify revision, Chrome executable/version, Chrome process state, and a bounded stderr tail. It SHALL NOT dump the process environment or credentials.

#### Scenario: Target discovery times out

- **WHEN** Chrome does not answer Target.getTargets within 15000 ms
- **THEN** visual-check records a machine-readable target-discovery timeout with the Chrome and CDP evidence above

#### Scenario: Chrome exits or the CDP pipe breaks

- **WHEN** Chrome exits or a CDP pipe reports an error before the target response
- **THEN** visual-check retains the existing process/pipe failure classification and does not relabel it as the target-discovery timeout

### R2. Only the read-only target-discovery timeout receives one bounded retry

Archify SHALL issue at most one additional Target.getTargets request after a 1-second delay, on the same browser process and CDP pipe, and only when the first Target.getTargets request timed out and Chrome remains alive. Other CDP timeouts, launch errors, parse errors, and content failures SHALL NOT be retried.

#### Scenario: The first target query recovers

- **WHEN** the first Target.getTargets request times out and the second request returns a page target
- **THEN** visual-check proceeds with the normal complete inspection and records that one recovery attempt occurred

#### Scenario: The second target query also times out

- **WHEN** both Target.getTargets requests time out
- **THEN** visual-check exits nonzero with both attempt records and does not report a pass

#### Scenario: A content inspection fails

- **WHEN** a viewport, readability, native interaction, or capture assertion fails
- **THEN** the failure remains nonzero and no startup retry is attempted

### R3. A recovered startup timeout does not weaken visual acceptance

A pass SHALL require the original exact checked-in HTML hash, all existing Archify viewport/readability checks, all required captures, and the existing native navigation and graph interaction browser review. A recovered timeout MAY add a warning diagnostic but SHALL NOT skip any check.

#### Scenario: Startup recovers and all evidence is produced

- **WHEN** target discovery recovers on its single retry and all existing visual checks pass
- **THEN** the workflow records the initial timeout and recovery, completes the native browser review, and uploads the existing receipt and capture evidence

#### Scenario: Startup recovers but a visual assertion fails

- **WHEN** target discovery recovers but any visual or interaction check fails
- **THEN** the workflow remains failed with that assertion and its diagnostic

### R4. A failed inspection preserves actionable evidence

The workflow SHALL retain the visual-check receipt, per-attempt log, pinned Archify revision, Chrome and Node versions, runner image/OS/kernel identity, and bounded load/memory/disk context in its always-uploaded artifact. The GitHub step SHALL remain failed when visual-check fails.

#### Scenario: The browser check fails

- **WHEN** visual-check exits nonzero
- **THEN** the artifact is uploaded with the failure receipt, the runner context, and the complete command log, and the required check remains red

### R5. Runner, Chrome, and Archify evidence stays separated

The receipt SHALL identify the Archify revision and Chrome process result. The workflow artifact SHALL identify the hosted runner image and resource snapshot. The subsequent Playwright browser-review receipt SHALL continue to identify its own browser version. No one layer's success SHALL be treated as proof that another layer passed.

#### Scenario: The two browser paths complete

- **WHEN** Archify visual-check and the native browser review both finish
- **THEN** the artifact records each tool/browser result separately with its existing capture and interaction evidence

## Exclusions

- No disabling Chrome sandbox/AppArmor, no broad timeout increase, no blanket retry, no continue-on-error, no skipped workflow step, and no conversion of a runtime error into a pass.
- No Architecture HTML content edits, CLI behavior changes, #966 registry CAS changes, or #987 main-probe changes.
- No merge, runtime install, deployment, or Cortex intake in this planning PR.
