---
status: accepted
work_item: architecture-html-cdp-timeout
issue: 1084
---

# Architecture HTML Chrome CDP target timeout 設計（#1084）

## Context

Issue #1084 records two failures on different pull request heads in the same Architecture HTML workflow step. Both receipts report viewer/visual-check-runtime with Target.getTargets timing out after 15000 ms, before viewport/capture evidence exists. Both same-head reruns passed the Architecture HTML check. The checked-in HTML did not change in either candidate.

The current workflow checks out a fixed Archify revision, selects the runner's Google Chrome with ARCHIFY_CHROME, runs Archify visual-check, then runs tests/architecture_browser_review.py. The latter uses Playwright's installed Chromium and verifies file navigation, SVG nodes/edges, controls, and screenshots. These are separate browser paths and must remain separate evidence.

At the pinned Archify revision, PipeCdp.send rejects a timeout without calling failureDetails. That callback includes Chrome exit/signal state and captured stderr for pipe/process failures. runVisualCheck catches the bare timeout as viewer/visual-check-runtime, persists the receipt, and closes the browser, which terminates Chrome and removes its profile. The current GitHub artifact therefore preserves the generic error but cannot recover the discarded process context afterward.

## Goals / Non-Goals

**Goals:**

- Classify the exact Target.getTargets timeout without claiming its root cause before evidence exists.
- Permit one bounded retry of that idempotent discovery request while retaining both attempt records.
- Keep every existing Architecture HTML content, navigation, graph-interaction, and capture gate fail-closed.
- Make a failed hosted-runner attempt distinguishable across runner context, Chrome process state, and Archify behavior.

**Non-Goals:**

- Change architecture.html, architecture.json, or Architecture HTML visual requirements.
- Change #966 registry revision CAS or #987 main-probe behavior.
- Disable Chrome sandboxing, skip a step, tolerate a visual assertion failure, or retry arbitrary runtime errors.
- Publish or deploy an Archify release or merge implementation in this planning PR.

## Decisions

### D1. Fix the diagnostic seam in Archify

The CDP timeout originates inside the pinned Archify process. Update the upstream Archify request/error contract so a timeout includes method, timeout, elapsed duration, attempt number, browser identity, process state, and bounded stderr. Emit a stable diagnostic code for Target.getTargets only. Avoid logging environment variables or unbounded Chrome output.

The timeout branch must capture process details before close() terminates Chrome and removes its temporary profile. A workflow-only post-failure ps snapshot cannot reconstruct stderr already consumed by the engine.

### D2. Retry only Target.getTargets once

Target.getTargets is a read-only discovery call used by attach(). On its first 15000 ms timeout, wait one second and send the same request once on the existing CDP pipe while Chrome remains alive. This avoids restarting the whole workflow or browser and tests whether a delayed browser response was the transient event. Do not retry any other command, Chrome exit, broken pipe, HTML inspection, screenshot, or native interaction failure.

If the second request also times out, return nonzero and include both attempt records. If the first request eventually replies after its timeout, its stale request id is ignored by the pending-request map and cannot satisfy the second request.

### D3. A retry is diagnostic and bounded, never an acceptance bypass

A recovered request allows the ordinary visual-check sequence to begin. The final pass still depends on all existing Archify checks and captures plus tests/architecture_browser_review.py. The receipt records the first timeout as a recovery warning; it does not set a pass until all existing checks pass. A persistent startup timeout remains a required-check failure.

### D4. Capture evidence at the layer that owns it

The upstream receipt records the Chrome/Archify/CDP details available at timeout. The Cortex workflow records only an allowlisted runner snapshot: run id/attempt, RUNNER_OS/RUNNER_ARCH and image identity when available, kernel, Node version, ARCHIFY_CHROME path/version, load, available/cgroup memory, and /tmp disk capacity. Capture visual-check stdout as JSON and stderr/command output as a separate log so the JSON remains parseable. Keep the existing if: always() artifact upload and include these files in architecture-native-review.

Do not write the full environment, credentials, arbitrary process command lines, or unbounded logs to the artifact. The later Playwright browser-review.json continues to report its own browser version.

### D5. Respect repository ownership

The pinned source is maintained in tt-a1i/archify. That repository must publish and review the diagnostic/retry change first. paulsha-cortex then updates the exact Archify commit pin and adds runner/log capture. Do not vendor or patch the checked-out upstream file from this repository. If the upstream change is unavailable or the evidence points to a runner-image issue, stop and obtain a separately authorized scope decision rather than weakening the required check.

### D6. Keep CI exit and artifact semantics explicit

The visual-check command remains required and its exit code propagates after writing diagnostics. The artifact upload still runs after failure. The native Playwright review only runs after successful Archify inspection; a retry is not permission to skip it. Preserve the current exact HTML rebuild comparison and both browser paths.

## Risks / Trade-offs

- A same-process retry may not recover a Chrome startup failure → one retry only; second timeout remains red with process and stderr evidence.
- Adding a warning diagnostic may affect receipt consumers → keep schema version compatible, add the recovery record additively, and test current consumers.
- Hosted runner image identity may not be available as a stable environment variable → record supported runner labels and kernel/resource snapshot; do not infer a runner cause from missing data.
- Upstream ownership may delay the local workflow change → keep the dependency explicit; do not patch or vendor Archify in the Cortex repository.

## Migration Plan

1. In the Archify repository, add the typed timeout, one retry, receipt evidence, and fake-CDP tests; publish a reviewed commit.
2. In paulsha-cortex, update only the pinned Archify ref and wrap the existing visual-check invocation to save runner context plus stdout/stderr without changing its required exit semantics.
3. Run the existing architecture documentation/phase-dispatch tests and the actual Architecture HTML workflow. Confirm both a recovered startup path and a non-recoverable/content-failure path remain red/green as specified.
4. Roll back by restoring the prior pinned Archify commit and the workflow diagnostic wrapper together if the new receipt or retry changes the visual-check contract unexpectedly.

## Open Questions

None. The cause remains unknown by design; the selected contract classifies only the observable target-discovery timeout and preserves enough evidence for follow-up diagnosis.
