---
status: accepted
work_item: retry-build-pinned-main-merge-task
---

# retry-build 固定 C/M merge task aggregate 規格（#973 Child B1）

## Requirements

本票是 #973 Child B 的 Red task/transport aggregate，runtime integration 依賴 #972 的 live chain #987→#988→#989→#990。B1 保留 operator retry-build 與 Manager automatic caller 共用固定 C/M task 的完整 AC；實作由三張 issue-backed Yellow children B1a/B1b/B1c 分別完成 Manager source pin、Builder clone object import、task construction。B1 本 aggregate 不重複寫各子票實作，也不下修驗收。

### R1 Exact tuple and Candidate CAS
Operator `work_actions._retry_build_action` and Manager automatic caller SHALL use the same-run typed `main_sync` context for exact Candidate C、pinned M、repair kind and complete conflict paths. `run.candidate_head == C`; C/M SHA/context must validate against B1a Manager source pin before reset/dispatch; the per-job Builder private ref is created only after reset by B1b and must be verified before launch. Missing context/source object, malformed values or Candidate CAS mismatch MUST refuse before reset/dispatch and MUST NOT use caller data or floating `origin/main`.

### R2 Transfer exact M to the isolated Builder clone
The #972 probe's M object exists in Manager's ship/source clone but may be absent from the independent Builder clone. B1a SHALL pin exact M in an advertised attempt-scoped Manager source ref using a stable pre-reset token. B1c constructs the task and deterministic expected Builder private-ref name before reset. After reset, B1b explicitly fetches exact M from the Manager source path into the independent Builder clone and verifies the private ref before launch, while keeping HEAD/feature/base at C; only the import receipt binds the allocated job id. No Builder fetches main; an M→N movement cannot substitute N.

### R3 One shared pinned task
The operator path and B2 automatic caller use one B1c task helper. Task/job input binds run, C, M, repair kind, all conflict paths, private ref and the deterministic B0 reservation id where applicable. #989 manual authority remains distinct; automatic path does not fabricate operator adjudication.

### R4 Real merge and allowed conflict contract
Builder SHALL produce a true non-fast-forward merge D with ordered parents `[C,M]`. Only clean-behind and the one classified CHANGELOG `[Unreleased]` insertion conflict are eligible. Keep all full entries from both sides exactly once, Candidate entries first, and preserve ordinary non-conflicting M changes. Other conflict paths stop with complete path list.

### R5 Authority boundary
Builder alone authors D. B1 only transports M and constructs task input; B3/B4 own proof/adoption/ref CAS. It does not prove prompt compliance, mutate Candidate, harvest D, run gates or push.

## Verification

With a bare origin, show Manager source has M while Builder clone initially lacks it; construct the B1c action while the Builder private ref is absent; after reset explicitly fetch/import exact M and verify the private ref before launch, keep checked-out branch at C, and prove M→N does not replace M. Missing M in Manager source fails before reset/dispatch. Test task helper parity between manual and automatic callers and exact C/M/private-ref binding. B3 validates actual Git `[C,M]` parents/tree/content; no prompt-only test counts as merge proof.

## Boundary and sizing

B1 aggregate spans `manager.py`, `seams.py` and `work_actions.py` and a cross-clone durable object handoff, so domain=1/state=2. Accepted fix-standard artifacts compute 7 / Red. Implementation dispatch belongs to accepted Yellow children B1a/B1b/B1c; keep full B1/#973 AC intact.
