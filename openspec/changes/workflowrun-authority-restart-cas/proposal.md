---
status: draft
work_item: workflowrun-authority-restart-cas
issue: 1068
parent_issue: 1055
openspec_change: workflowrun-authority-restart-cas
domain_breadth: 0
state_consistency: 2
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
applicable_contract_rules:
  - R-09
  - R-16
  - R-19
---

# 既有 Candidate WorkflowRun authority restart CAS 規格（#1068）

本規格是 owner child [#1068](https://github.com/hamanpaul/paulsha-cortex/issues/1068)、parent [#1055](https://github.com/hamanpaul/paulsha-cortex/issues/1055) 的規劃草稿。唯一 work item 與 own OpenSpec change 都是 `workflowrun-authority-restart-cas`。本輪只發布規劃文件；不代表內容已 accepted、run 已 frozen、issue 已 intake 或產品已實作。

## 範圍

本 child 只定義 `paulsha_cortex/coordinator/registry.py` 中單一、domain-specific 的 WorkflowRun authority-restart transition。它消費 #966 提供的 durable raw-byte revision CAS，對同一個既有 Candidate run 驗證 exact WorkflowRun snapshot，並在一次 durable transaction 內更新該 run 的 claim/source era 與 verify/review authority。

本規格不定義 Todo/WorkAuthority 的解析或資格驗證、不負責 Manager 的 operator-resume 決策或 dispatch 次序，也不擁有 delivery journal。這些責任依 #1054、#1063–#1065、#1069 與 #1070 的分工處理。#983 的 run、Candidate 與 PR 值只可作隔離 fixture；本規格不授權讀寫 live #983 run 或 PR #1049。

## 依賴與凍結門檻

- **實作硬阻擋：必須先合併 #966。** #966 提供通用 JobRegistry durable revision CAS、跨 process transaction lock、`RegistryRevisionConflict` 與 conflict memory recovery。#1068 只消費已合併 API；不得複製 raw-file writer、lock、revision algorithm，或把未合併的 PR #1067 當成已交付 API。
- 本輪查核時 #966 與 PR #1067 仍為 OPEN；因此本規格明確禁止產品實作與 intake。合併後須依實際 landed API 重新核對本規格；若 #966 沒有可安全支援單次 tuple-check-and-transition 的 registry seam，便停止並提出 issue-backed re-scope，不讀寫私有 revision 欄位，也不另造 writer。
- Manager integration #1069 仍須等 #1054、#1063、#1064、#1065 與本 child 的正式依賴條件完成。這些 caller-side readiness 不在 #1068 registry API 內重複實作。
- #1055 是 aggregate；#1068 完成不代表 #1055 完成，也不代表 #983 可恢復或交付完成。

## 規格需求

### R1 — Request 與 WorkflowRun snapshot 必須完整且精確

Registry transition MUST 收到 expected durable registry revision、目標 run 的完整 expected snapshot、含完整 request payload 的唯一 request identity，以及 caller 已驗證的新 WorkAuthority digest/binding。Expected run snapshot MUST 包含 `run_id`、repo、`work_id`、status、phase、retry classification、舊 `claim_key`、舊 `source_revision`、Candidate、`verified_head`、verify/review gate 值與 evidence refs、PR refs，以及觀測到的 no-active-job 狀態。新 authority digest MUST 由已驗證 WorkAuthority 的 caller 以完整單一值提供；任意 patch 欄位或縮短的 digest MUST 拒絕。

#### 情境：caller 提供完整的目前 binding

- **WHEN** Manager 提供 exact current run/job snapshot、expected raw registry revision 與完整的新 authority digest
- **THEN** registry 可精確評估單次 transition，且不重新解析 Todo、WorkAuthority、GitHub 或 filesystem sources

#### 情境：必要 tuple 欄位缺漏

- **WHEN** caller 省略必要的 run、Candidate、gate、evidence、PR、active-job 或 authority-digest 欄位
- **THEN** transition 必須在 durable mutation 前拒絕，並回報 typed conflict/validation error

### R2 — 單次 exact revision compare-and-transition

Registry MUST 在 #966 CAS boundary 內，以同一份 durable snapshot 比對 expected raw durable revision 與每個 expected WorkflowRun/job 欄位。只有完全相符時 MAY 更新同一個 run。Transition MUST 在單一 durable registry revision 中提交新的 claim/source binding、`phase=verify`、verify/review invalidation、清除後的 `verified_head`，以及唯一一筆 authority-restart audit。任何 raw-revision 或 tuple drift MUST fail-closed；MUST NOT 合併欄位、自動重試或重播部分 mutation。

#### 情境：exact current snapshot 只 transition 一次

- **WHEN** durable revision 與所有提供的 tuple 欄位相符，目標為同一個 ongoing Candidate run，且沒有 active job
- **THEN** registry 在同一個 run 上以單一 durable revision 提交完整 authority restart

#### 情境：durable revision 或 tuple 已變更

- **WHEN** 另一個 registry writer 改變任一 byte，或任何 expected run/job 欄位不同
- **THEN** request 收到 typed conflict，不新增 audit 或 gate mutation，也不持久化 stale snapshot

### R3 — 僅失效指定 gate，並保留歷史資料

成功的 restart MUST 只失效目前 Candidate 的 verify/review authority：清除 `verified_head`、將既有 verify/review gates 標示為待重新評估，並依已驗證的新 authority 推進目前 claim/source binding。它 MUST 保留 Candidate、build/repair steps、builder jobs 與 evidence、舊 claim-era JobRegistry bindings、PR refs、run identity 與既有 history。MUST NOT 建立 replacement run、修改 build evidence、重設無關 gates 或 dispatch job。

#### 情境：既有 Candidate 的 authority 前進

- **WHEN** Candidate-bearing verify/review run 的 exact CAS 成功
- **THEN** 同一個 run 保留全部 build 與 delivery identity，而 verify/review evidence 對新的 claim era 失效

#### 情境：restart request 遇到 active job 或缺少 Candidate

- **WHEN** 存在 active job，或 exact target 沒有 Candidate
- **THEN** registry 拒絕 restart，並保留既有 run 與 evidence

### R4 — Request identity 必須具冪等性

Registry MUST 在包含單筆 audit 的同一 durable revision 中保存 request identity 與完整 transition payload 的 digest。重送相同 request identity 與相同 payload MUST 回傳先前 transition 結果，不可再次寫入、增加 attempt/audit 或清除較新的 verify/review evidence。以不同 payload 重用 identity MUST 回傳 typed conflict。不是 exact replay 的 stale/concurrent request MUST 回傳 typed conflict。

#### 情境：registry restart 後 exact replay

- **WHEN** registry reload 後重送相同 request identity 與完整 payload
- **THEN** 回傳原結果，durable revision、audit count 與目前 gate evidence 均不變

#### 情境：identity 被重用於不同內容

- **WHEN** 已存在的 request identity 改用不同 run、digest、tuple 或 transition payload
- **THEN** registry 回傳 typed request-content conflict，且不執行 mutation

### R5 — Conflict 與持久化失敗安全

Stale revision、tuple drift、錯誤 run/repo/work/claim/source/Candidate/gate/evidence/PR 值、active job、identity 重複但 payload 改變，以及 durable write failure MUST 各有 deterministic failure path。遭拒的 transition 不可將嘗試設定的 claim/source binding、已清除的 gates、變動後的 `verified_head` 或 audit 留在 durable state 或 registry memory。Conflict recovery MUST 遵循 #966 landed 的 durable-snapshot reload/restore contract；失敗 MUST NOT 回報為 transition 成功。

#### 情境：transition 期間 persist 失敗

- **WHEN** 任一階段的 validation 或 atomic persistence 失敗
- **THEN** durable snapshot 不包含 transition，不回報部分 restart；仍以既有 registry rollback/reload semantics 為準

### R6 — 使用隔離 registry fixtures

Implementation MUST 使用真實 `JobRegistry` fixtures，並為正向與負向案例同時核對 raw registry bytes 與 memory state。覆蓋範圍 MUST 包含一次成功 transition、stale raw revision、錯誤 run/claim/source/Candidate/gate/PR tuple、active job、persistence failure、exact replay、同 identity 不同 payload、fresh registry reload 後 replay，以及 #1069 指定的 #983 fixture identity/Candidate 值。這些值只作 fixture。Tests MUST NOT 讀取、修改、resume、dispatch 或以其他方式接觸 live #983 run `workflow-52d048b72adbd5cae06f`、Candidate `7ba7e877c94ff4eee72ba796ea9f8962953ed5cc` 或 PR #1049。

### R7 — 檔案、owner 與 delivery 邊界

Production changes MUST 限於 `paulsha_cortex/coordinator/registry.py`；可新增或更新 tests。若 landed #966 contract 或實作證據要求修改第二個 production module，停止並提出 issue-backed re-scope。#1068 MUST NOT 修改 `work_actions.py`、Manager/WorkAuthority 或 GitHub 行為、delivery journal owner、#983 live state、push/PR/merge 行為、pre-Candidate abandon/recovery，或任何 builder/verify/review dispatch。

#### 情境：實作越過 owner 邊界

- **WHEN** 提案變更需要 Manager `work_actions.py`、delivery journal、GitHub 或另一個 production module
- **THEN** 停止本 child，將需求交給指定 issue/owner，不擴大 #1068 範圍

## 非目標

- Todo source discovery、Todo qualification、Monitor freshness、WorkAuthority digest 建構，或 operator permission/order。
- Builder/build retry、Candidate 建立/adoption、verify/review job dispatch，以及任何 periodic recovery behavior。
- Delivery journal CAS/reconciliation、remote PR 操作、merge/closeout，或 #983 live recovery。
- Generic registry writer、field patch API、自動 merge/retry，或第二套 revision/lock mechanism。
