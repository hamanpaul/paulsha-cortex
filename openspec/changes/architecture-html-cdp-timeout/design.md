## Context

The Architecture HTML workflow currently checks out tt-a1i/archify at a fixed commit, selects the hosted runner's Google Chrome, runs visual-check, then runs a separate Playwright browser review. On two different pull request heads, visual-check reported Target.getTargets timing out after 15000 ms before any viewport or capture was produced. Same-head reruns passed the Architecture HTML check, but GitHub hosted reruns do not prove the same physical runner was reused.

At the pinned Archify revision, PipeCdp.send rejects a timeout without failureDetails. The details callback contains Chrome exit/signal and stderr for pipe/process errors. The timeout is then converted to viewer/visual-check-runtime; the finally path closes Chrome and removes the temporary profile. The local workflow cannot recover that in-process evidence after the child exits.

## Goals / Non-Goals

**Goals:**

- Classify and preserve evidence for the exact target-discovery timeout.
- Retry the read-only Target.getTargets request once, with a fixed bound.
- Keep the original visual and interaction gates required.
- Separate runner, Chrome, Archify, and subsequent Playwright evidence.

**Non-Goals:**

- Change the checked-in architecture HTML or its generated source.
- Treat a rerun as a pass, skip a required step, disable sandboxing, or broadly extend timeouts.
- Change #966/#987 product logic or work lifecycle.
- Merge, install, or deploy the implementation from this planning-only change.

## Decisions

### D1. Upstream Archify owns CDP classification and browser evidence

Add typed timeout metadata and process/stderr evidence where the CDP request is made. Identify only Target.getTargets at the existing 15000 ms timeout as retryable. A bounded stderr tail and explicit Chrome process state must be captured before browser cleanup.

### D2. Retry the discovery request once on the existing browser

After the first exact Target.getTargets timeout, wait one second and retry on the same CDP pipe only if Chrome is still alive. Do not retry another method, an exited process, a pipe/launch error, or a visual assertion. A second timeout remains a nonzero failure with both attempt records.

### D3. Cortex updates only the integration and runner evidence

After an upstream reviewed immutable commit exists, update the workflow pin and collect an allowlisted runner snapshot, Node version, Archify revision, Google Chrome path/version, load, memory, and temporary-disk context. Save receipt JSON and command log separately, preserve the required exit code, and keep the existing always-uploaded artifact.

### D4. Preserve every acceptance gate

A recovered target query proceeds through the complete Archify visual-check, exact checked-in HTML comparison, and existing Playwright browser review. A retry warning does not count as a visual pass. Any later visual or interaction error stays red.

## Risks / Trade-offs

- Same-process retry may not recover a persistent browser startup failure → the second timeout fails with first/second attempt diagnostics.
- Additive diagnostic fields could affect receipt consumers → retain schema version and add explicit compatibility tests.
- Upstream delivery may be delayed → do not patch or vendor the external source from the Cortex repository.
- Runner image metadata may vary by hosted image → record available image identifiers and kernel/resource context; do not infer a cause from absent fields.

## Rollout and rollback

1. Review and publish the Archify diagnostic/retry change with fake-CDP coverage.
2. Pin the exact upstream commit and add local runner/log evidence.
3. Verify retry recovery and exhausted/content-failure paths in the actual Architecture HTML workflow.
4. Roll back the pin and local wrapper together if the new receipt or retry changes existing visual-check behavior.
