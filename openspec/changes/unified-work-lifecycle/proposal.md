## Why

Monitor目前只投影repo內stage文件，Manager則只追蹤已派工Job/Slice；GitHub issue、Todo/spec/plan、active OpenSpec、workflow、PR/review/check與remote archive沒有共同、可解釋且fail-closed的生命週期。這會讓issue存在、artifact存在、agent exit與可信完成被錯誤等同，也無法安全支援跨repo manual/label claim。

Umbrella issue `hamanpaul/paulsha-cortex#14` 已將問題定義為 `topic → todo → on-going → done` 的閉合鏈。本change建立read model、persona workflow與delivery gate的共同契約，並分四個code PR落地，不把新範圍塞入issue #10。

## What Changes

- Monitor新增GitHub、repo artifact、workflow/completion providers；使用per-provider durable last-good snapshot與degraded freeze。
- 新增`WorkItem`、`WorkSource`、`WorkflowRun`、`WorkflowStep`，固定四態lifecycle、phase與facets語意。
- 固定repo override為`.cortex/work-items.yaml`、markdown關聯key為`work_item`；confirmed與inferred association分離，collision fail-closed。
- 新增`cortex list`與`cortex work show|link|unlink|start|resume|auto`，並固定`cortex-work/v1` JSON schema。
- Monitor socket新增work-item list/get/explain/subscription；ProjectState API相容一個release cycle。
- 新增planner persona、workflow manifest persona binding、registry schema v2 migration、`agy` safe plan launcher與異質brainstorm completeness gate。
- Manager新增manual/label claim、preflight/Copilot/thread/check/merge gate、remote archive與merge後strict closure；`needs_human`/`blocked`/`degraded`保持facets。
- 新增`cortex doctor --probe-live`、migration/service/help/docs與docs-only live canary。

## Capabilities

### New Capabilities

- `unified-work-read-model`: 跨GitHub/repo/workflow來源建立可解釋、可回退、provider-degraded-safe的Work Item read model與CLI/socket API。
- `persona-workflow-orchestration`: 以Manager single-writer持久化WorkflowRun/Step，保存persona binding並執行異質brainstorm/build/review流程。
- `governed-delivery-closure`: 以policy/preflight/checks/current-HEAD review/threads/remote archive/merge後evidence閉合delivery，只有全部成立才done。

### Modified Capabilities

無。既有`trusted-dispatch-completion`維持Slice Candidate verification/ForeignReview語意；本change在其外層新增Work Item workflow與GitHub terminal delivery。

## Impact

- Monitor：`paulsha_cortex/monitor/**`新增providers、correlation、durable snapshot與work socket projection。
- Workflow：`paulsha_cortex/coordinator/**`、`deck/**`、`persona/**`新增registry v2、planner/agy、claim與ship orchestration。
- Public API：top-level CLI/help、Unix socket、`.cortex/work-items.yaml`、`work_item` frontmatter、`cortex-work/v1` JSON。
- State：新增`$PSC_MONITOR_STATE_ROOT/work-items.snapshot.json`；coordinator registry v1原子備份後升v2，舊records保留legacy但不自動關聯。
- GitHub：v1 authenticated `gh api`為canonical，label固定`cortex:auto-on-going`；只支援GitHub terminal delivery。
- Related issues：#4、#5、#8由相應phase實作；#10只清stale artifact後依證據處理；#12只勾實際完成項目。
- Compatibility：ProjectState API保留一個release cycle；auto claim default off，canary通過前不擴散label。
