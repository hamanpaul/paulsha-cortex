---
status: draft
work_item: monitor-refresh-attempt-ledger
issue: 1077
domain_breadth: 1
state_consistency: 1
invariant_count: 4
artifact_classes:
  - source
  - tests
  - documentation
---

# Monitor refresh attempt ledger 工作計畫（#1077）

## Binding and dependency

- 唯一 owner：[#1077](https://github.com/hamanpaul/paulsha-cortex/issues/1077)，唯一 `work_item`：`monitor-refresh-attempt-ledger`。Superpowers spec/design/todo 與 OpenSpec proposal/design/tasks 均使用此 binding。
- Parent：[#1064](https://github.com/hamanpaul/paulsha-cortex/issues/1064)。Draft PR [#1072](https://github.com/hamanpaul/paulsha-cortex/pull/1072) 的 parent work item `monitor-correlation-refresh-generation` 維持獨立，不與本 child 共用。
- Hard order：#1063 canonical Todo/source/path contract → #1077 attempt ledger → #1078 success evidence/freshness API → #1064 umbrella completion → #1065 WorkAuthority consumer → #1054 Manager admission。

## Boundary

只修改 `paulsha_cortex/monitor/work_api.py` 的 refresh orchestration、`work_snapshot.py` 的 durable marker storage 與 focused tests。每次 refresh 先寫嚴格遞增 generation/`running`；逐 repo 保存 failure、degraded、exception outcome；marker unknown、corrupt 或 I/O failure 不得回退。Last-good `WorkSnapshot` 可保留作診斷。

本票不捕捉成功 input/source revisions、不做 snapshot read-back、不提供 freshness API；這些由 #1078 承接。也不重做 #1063 qualification/path admission、不接 #1065 WorkAuthority、不做 #1054 Manager gate、CLI、recovery、ship、formal intake 或 deployment。

## Five-dimension sizing

Sizing 使用官方 `current_sizing_snapshot()` 與 `fix-standard`，並把此 child 自有 OpenSpec change 一起納入；六個 helper rows 共用 `work_item: monitor-refresh-attempt-ledger`：

| Dimension | Draft score | Basis |
| --- | ---: | --- |
| `domain_breadth` | 1 | 同一 Monitor 責任域，預期觸及 `work_api.py` refresh orchestration 與 `work_snapshot.py` marker store 兩個 production modules。若再需第三個 production module，先重算。 |
| `state_consistency` | 1 | 增加獨立 generation/outcome marker，並與 last-good snapshot 明確分離；不在本票綁定 snapshot payload、source revisions 或 consumer authority。 |
| `acceptance_surfaces` | 2 | Repo `fix-standard` 的 2 個 gate-spine 加上固定適用的 R-09/R-16/R-19，signal=5，計為 2。 |
| `spec_stability` | 2 | 目前六份 artifact 均為 draft；官方 completeness 不完整。 |
| `orchestration` | 2 | Repo `fix-standard` 有 9 張 cards，9 張均有 `persona_binding`。 |
| **Current draft** | **8 / Red** | `current_sizing_snapshot()` 實算；保留完整 issue acceptance，不把這個分數描述為可 intake。 |

Official helper rows：

- `docs/superpowers/specs/monitor-refresh-attempt-ledger-spec.md` — `spec`
- `docs/superpowers/specs/monitor-refresh-attempt-ledger-design.md` — `design`
- `docs/superpowers/workstreams/monitor-refresh-attempt-ledger/todo.md` — `plan`
- `openspec/changes/monitor-refresh-attempt-ledger/proposal.md` — `spec`
- `openspec/changes/monitor-refresh-attempt-ledger/design.md` — `design`
- `openspec/changes/monitor-refresh-attempt-ledger/tasks.md` — `plan`

只在暫存副本把上述六份 frontmatter `status` 改為 `accepted`，官方 completeness projection 為 complete、missing kinds 為空，計分為 **1+1+2+0+2 = 6 / Yellow**。此為 accepted-status counterfactual，不代表本文件已接受、issue 已可 intake 或實作已授權。若實際 scope、combo、production modules 或 helper rows 變更，須重跑 sizing；不得以縮小原 issue 接受條件規避 Red。

## Tasks

- [ ] **T1 — Isolated marker tests / RED**：以 temp snapshot/marker stores 與 fake providers/fake clock 驗 generation 嚴格遞增、每次 provider scan 前已 durable `running`、同 repo failure 後 last-good rows 保留但 latest outcome 不可信。
- [ ] **T2 — Durable allocation**：建立 strict versioned sidecar、明確首次 empty-store 初始化、atomic durable write；unknown/malformed、marker read/write failure、已有 snapshot 卻缺 marker 時 fail closed，不回退零代。
- [ ] **T3 — Refresh integration**：在 `WorkModelRefresher.refresh()` 的 provider/correlation 工作之前配置 generation；將每個 repo 的 failure/degraded/exception 以獨立 outcome 寫入 marker，marker write 失敗保持 untrusted。
- [ ] **T4 — Crash/restart and ownership**：測 crash 留下 running、restart 不倒退、unknown/corrupt marker 與讀寫錯誤；驗同 instance 唯一 writer，若 store 可跨 process 共用則使用 durable lock/CAS 或拒絕第二 writer。
- [ ] **T5 — #1078 handoff and boundary**：scan 完成但缺成功證據時仍是不可信狀態；成功 manifest、exact input/source capture、snapshot durable read-back 與 freshness API只留給 #1078；不得新增 WorkAuthority、Manager、CLI 或 recovery/ship 接線。
- [ ] **T6 — Documentation and delivery**：同步必要 Monitor docs、changelog fragment 與 `CHANGELOG.md [Unreleased]`；跑 focused/full tests、strict OpenSpec、repo preflight 與 actual PR-context policy；逐項記錄未執行或失敗的 gates。這些是未來產品實作驗收，不是本 planning PR 的通過宣稱。

## Planning PR status

此分支只建立規劃 artifact；不執行正式 Cortex intake、不建立 runtime run、不實作 code、不關閉 #1077，也不宣稱 #1064/#1065/#1054 完成。Draft PR 的 `policy-exempt:issue-link` 只用於避免規劃 PR 關閉仍待產品交付的 issues。
