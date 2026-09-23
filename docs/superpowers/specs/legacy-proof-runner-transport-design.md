---
status: accepted
work_item: legacy-proof-runner-transport
issue: TBD
domain_breadth: 1
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# G — fixed Builder runner 設計

## Decisions

- **D1** — Use a fixed root-owned broker/unit, not a generic command runner. Bind output to the actual unit invocation and OS identity. Extend the managed service installation lifecycle; if the deployment cannot provide authenticated read-only execution, fail closed.

## Dependency and failure boundary

Blocked by F and E. Uses the existing controlled cortex install service lifecycle; no Manager direct traversal. 缺少任何契約、owner、權限、lineage、proof 或 exact target 時，一律 fail closed；不得以同名 row、caller claim、fixture shortcut 或舊 marker補足。

## Five-dimension sizing

依 repo fix-standard 与 current_sizing_snapshot helper 計分：domain_breadth=1、state_consistency=1、acceptance_surfaces=2（2 gate-spine 加 R-09/R-16/R-19）、spec_stability=0（本三件套不含 open-question blocker）、orchestration=2（9 cards/9 persona bindings），預期總分 6 / Yellow。Sibling todo 必須記錄實際 helper output；此分數不解除上游 hard dependency。

## Evidence and limit

job_workspace.py explicitly documents that Manager cannot traverse Builder 0700 clones. This proposal does not claim an installed trusted proof runner currently exists.
