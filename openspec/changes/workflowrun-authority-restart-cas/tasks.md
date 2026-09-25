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

# 既有 Candidate WorkflowRun authority restart CAS 工作清單（#1068）

## Owner 與唯一 binding

- Owner child: [#1068](https://github.com/hamanpaul/paulsha-cortex/issues/1068); parent/aggregate: [#1055](https://github.com/hamanpaul/paulsha-cortex/issues/1055).
- 唯一 `work_item` 為 `workflowrun-authority-restart-cas`，唯一 OpenSpec change 也為 `workflowrun-authority-restart-cas`。本組唯一 binding 同時寫在本 Todo、owner spec、owner design 及三份 OpenSpec view 的 frontmatter。不得為此 child 另建 Todo、change ID 或競爭的 authority。
- Canonical planning pair 為 `docs/superpowers/specs/workflowrun-authority-restart-cas-spec.md` 與 `docs/superpowers/specs/workflowrun-authority-restart-cas-design.md`。本 workstream Todo 是唯一 plan；own OpenSpec proposal/design/tasks 分別與 canonical spec/design/Todo byte-identical。
- 本基線狀態為 `draft`，尚未 accepted 或 frozen。發布 Draft PR 不會建立 Cortex mapping、正式 intake、dispatch authority 或產品 acceptance。所有未來產品 task checkbox 都保持未勾，直到各自取得證據。

## 範圍與硬性依賴

- #1068 只負責 `paulsha_cortex/coordinator/registry.py` 中單一 exact WorkflowRun authority-restart transition；未來測試只使用 fixtures。完整保留 live issue 的每項 acceptance condition。
- **#966 合併前不得開始 implementation。** 2026-09-25 規劃查核時，#966 為 OPEN，PR #1067 仍 OPEN，head 為 `0576f1ee285d99155b583a2532db31732040534`；GitHub 未回報 check runs。PR code 尚非 landed dependency 或穩定 API。合併後，implementation 前須重新讀取 exact merged head 與 public CAS surface。
- Registry transition 只消費已 landed 的 #966 durable revision CAS。MUST NOT 新增第二個 raw-file writer/lock 或使用 private revision field。若 landed API 無法在一次 durable commit 中共同保護 expected revision、完整 tuple 比對與 mutation，停止並提出 issue-backed re-scope。
- #1054 與 #1063–#1065 必須先提供 accepted Todo/admission/freshness/digest contract，才能進行 Manager integration。#1069 負責 `work_actions.py` explicit recovery permission/order；#1070 負責 delivery journal 工作。本 child 不改那些 modules，也不執行那些責任。
- #983 run `workflow-52d048b72adbd5cae06f`、Candidate `7ba7e877c94ff4eee72ba796ea9f8962953ed5cc` 與 PR #1049 只作 synthetic fixture values。不讀寫 live state、不存取 journal、不查 GitHub、不派工、不 push、不建立/merge/close PR。
- #966 是 #1068 implementation 的硬性依賴，與 #1055 aggregate 是否完成分開。未來只完成 #1068 implementation 不能關閉 #1055，也不能宣稱 #983 已交付。

## 七項不變量與 acceptance 對照

| 不變量 | 必要 contract | Live #1068 acceptance |
|---|---|---|
| I1 Request binding 精確 | Expected durable revision、完整 exact run/job tuple、完整 request identity/payload、caller 已驗證的完整 authority digest | AC1 |
| I2 單次 durable CAS | Exact revision/tuple check、同 run claim/source 更新、verify phase、verify/review invalidation、清除 `verified_head` 與單筆 audit 均在一次 commit | AC2 |
| I3 保留 build/delivery history | 保留 Candidate、build/repair steps、builder jobs/evidence、舊 bindings、PR refs、identity 與 history | AC3 |
| I4 Idempotent replay | 同 ID+payload 為唯讀 replay；payload 改變或 stale concurrent request 回 typed conflict | AC4 |
| I5 Failure safety | Stale revision、各類錯誤 tuple、active job、persistence failure 與 conflict 都不得留下 partial transition | AC2、AC5 |
| I6 真實 registry fixture evidence | Positive、negative、replay、restart 與 #983-shaped values 只以隔離 registry state 測試 | AC5 |
| I7 Owner boundary | Production diff 只限 `registry.py`；不改 Manager、journal、GitHub，不 dispatch，也不碰 live #983 | AC6、AC7 |

## 產品 Tasks — 等待 #966 landed 後才可開始

- [ ] **T1 registry API 與 source — I1/I2：**等 #966 合併後，將 domain-specific transition 綁定到其 public durable-revision CAS seam；接收完整 exact tuple 與已驗證的新 authority binding。Production diff 僅限 `paulsha_cortex/coordinator/registry.py`。
- [ ] **T2 atomic transition — I2/I3/I5：**拒絕任何 raw revision、target tuple、gate/evidence/PR 或 active-job mismatch。完全相符時，僅更新 current claim/source binding、將 phase 設為 verify、invalidate verify/review gates、清除 `verified_head`，並在單一 CAS transaction 中附加一筆 audit。保留 Candidate、build/repair history、builder jobs/evidence、舊 claim-era bindings、PR refs、run identity 與既有 history。
- [ ] **T3 replay identity — I4：**在同一 transaction 中保存 request ID 與完整 payload digest。確認 reload 後 exact replay 回傳原結果，不重新 persist、不增加 attempt/audit，也不清除較新的 gate evidence；相同 ID 搭配變更內容時回傳 typed conflict。
- [ ] **T4 registry fixtures — I5/I6：**新增隔離 `JobRegistry` tests，涵蓋一次成功、stale raw revision、各種錯誤 tuple、active job、persistence failure、同 ID exact/different-payload requests，以及 restart replay。#983 run/Candidate/PR 值只作 fixture。核對 raw bytes、memory、audit count 與 evidence 保留；不得讀取 live run。
- [ ] **T5 documentation 與 OpenSpec — I1–I7：**讓實作行為、tests、canonical spec/design/Todo 與 own OpenSpec delta 一致。保留唯一 work_item/change binding，不把 #1069 Manager 或 #1070 journal 責任搬入本 child。
- [ ] **T6 local gates — I1–I7：**實作後執行 focused registry tests 與 repo 要求的 tests；驗證 own OpenSpec change 與 canonical specs；帶 exact title/body/labels/base/head 執行 PR-context policy；記錄 exit code 與 final lines。若必要 test/gate 不可用，明確記錄，不勾選未完成 task。
- [ ] **T7 delivery evidence — I1–I7：**implementation owner review 後，分別記錄 exact-head CI、review 與 delivery 狀態。Tests 綠燈本身不會關閉 #1068；不得連帶 merge 或關閉 #1055/#983。

## 目前 sizing 與 gate 狀態

以本 Todo、對應 spec/design、`fix-standard` 與 repo 全套 process rules（`R-09`、`R-16`、`R-19`）執行 official `work_bridge.current_sizing_snapshot()`。Exact input refs 是「Owner 與唯一 binding」列出的三個 canonical paths；helper 結果為 `(8, "red")`。目前 artifact status 為 `draft`，因此 `stability-risk-v2` 正確將尚未 accepted 的 planning set 評為 risk 2。此 draft 實測結果與 issue 的 accepted-triad projection 分開記錄。

預期維度依據：

| 維度 | 分數 | 依據 |
|---|---:|---|
| `domain_breadth` | 0 | 預定 production owner 只有 `coordinator/registry.py`；若需第二個 module，必須停止並重定範圍。 |
| `state_consistency` | 2 | Durable raw revision CAS 加上 exact WorkflowRun tuple、audit、idempotent replay 與 rollback。 |
| `acceptance_surfaces` | 2 | `fix-standard` core gates 加上全部三項 process rules，acceptance signal 超過 score-2 門檻。 |
| `spec_stability` | draft 時為 2 | Helper 不接受 draft artifacts；尚未宣稱 planning acceptance 或 frozen authority。 |
| `orchestration` | 2 | `fix-standard` 有多張 persona-bound cards。 |

Draft score 為 8 / Red。三份 planning artifact 全部 accepted 且沒有 blocker 後，issue 現有 projection 為 0+2+2+0+2=6 / Yellow。這個 projection 不是產品證據，不會放寬 AC，也不會覆蓋 #966 或其他 intake gate。Implementation 前須以實際 linked combo 和當時 helper 重算；若為 Red，保留所有 acceptance criteria，只能透過 issue-backed 決策拆分。

## 僅規劃工作的驗證

- 本 planning PR 必須 strict validate `workflowrun-authority-restart-cas`、依 repository canonical command 執行 `openspec validate --specs`，並帶 exact PR context 跑 policy check。本 branch 只有 planning/changelog 文件，未實作 code，因此不跑產品 tests。
- `openspec validate workflowrun-authority-restart-cas --strict --no-interactive`：PASS（`Change 'workflowrun-authority-restart-cas' is valid`）。`openspec validate --specs`：PASS（27 passed、0 failed）。
- Intended PR context：title `docs(planning): 規劃 #1068 WorkflowRun authority restart CAS`；label `policy-exempt:issue-link`；base `main`；head `feature/1068-workflowrun-authority-restart-cas`。依規定執行 `python3 -m policy_check --repo . --pr-title ... --pr-body ... --pr-labels ... --pr-base-ref main --pr-head-ref ...`，結果為 23 pass、0 fail、2 warn、1 exempt skip。R-17 因本規劃 PR 有意讓 #1068 保持開啟而套用豁免。R-19 提醒 `architecture-html.yml` review step[3] 只命中註解/install line；R-22 以 advisory 回報 107 筆既有 dangling references。本 PR 沒有 workflow 或 test 變更。
- Official `preflight-ci` 加上 `--offline --skip-tests` 回傳 `PREFLIGHT PASS`：policy PASS、OpenSpec PASS、tests 明確 SKIP。此 docs-only planning change 未執行 manifest test step。
- 依 repo policy，`code_paths` 包含所有 Markdown，因此本 planning PR 也必須新增 changelog entry 與 fragment；它們不代表未來產品工作已完成。
- PR 維持 Draft 且不關閉 #1068。因 PR 引用 issue 但有意讓它保持開啟直到 implementation，Policy R-17 需套用 `policy-exempt:issue-link` label 並說明理由。
- `.project-policy.yml` 沒有宣告 `moc`，R-24 不適用。
