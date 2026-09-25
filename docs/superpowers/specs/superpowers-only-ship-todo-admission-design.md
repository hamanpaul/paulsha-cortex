---
status: draft
work_item: superpowers-only-ship-todo-admission
---

# Superpowers-only ship Todo admission 與恢復設計草案（#1051）

## Decisions

### D1 — 根因是 admission 少了 Todo source 前置條件

GitHub #1051 記錄的正式 run `workflow-52d048b72adbd5cae06f` 已完成 build、verify、review 並建立 PR #1049；WorkAuthority 當時為 PR=1、OpenSpec=0、Todo=0，ship 才停在 `multiple-delivery-targets-unsupported`。目前 `work_actions._ship_action` 的入口條件是 PR=1、Todo=1、OpenSpec=0/1；缺少 Todo 時的共用 diagnostic 卻建議 unlink。將相同需求前移到第一次 builder dispatch 前，才能避免先產生候選與 PR 才發現來源契約不足。

### D2 — WorkAuthority 是 Todo 的唯一授權來源

此草案不把 Superpowers plan 轉成 Todo。Superpowers `spec/design/plan` 的 `planning_authority` 由 run 持有；ship binding 與 remote closure 使用的是 WorkAuthority 的 `mapped_todo_paths`。兩個來源身份不同，靠檔名或 checkbox 把它們視為同一來源會跳過 owner、revision 和 correlation 證明。

若要補上 Todo，必須由 owner 將 canonical Todo path 透過受支援的 work link 納入 `.cortex/work-items.yaml`／來源設定，等待 Monitor snapshot 重新載入後，以新的 WorkAuthority 為準。link 寫入成功不等於 snapshot 已採信；同一個 intake 呼叫不得假裝已完成 refresh。

### D3 — 先 gate builder，再保留 ship backstop

在 Manager 從 plan／planning-complete 推進到第一張 builder 卡之前，讀取同一份 confirmed WorkAuthority，要求 `len(mapped_todo_paths) == 1`。零個時以 `missing-todo-source` 停在 planning admission；超過一個時以 `ambiguous-todo-source` 停止。停止結果要附 `work_id`、repo、mapping count、authority snapshot/revision 證據 ref 及合法 next action；不得配置 builder workspace/job 或執行 agent。

`_ship_action` 保留相同 exact-cardinality invariant，因為舊版 run、重播和其他 caller 仍可能到達 ship。零個與多個 Todo 分開分類：只有多個 mapping 才可提示 unlink；零個提示補一個由 WorkAuthority 擁有的 Todo source、等 snapshot refresh，之後由正式 resume/re-intake gate 判定可否繼續。

### D4 — #765 使「中途 link 後直接 resume」不能視為安全

#765 留下實例：ship 中途補 OpenSpec link 改變 WorkAuthority digest／claim key 後，歷史 job binding 不相符，resume 無法重播 evidence。#983 則已存在 exact candidate、verify/review 和 PR，現在若補 Todo source，同樣會推進 authority。僅刪除 journal stop 或原地改 revision 不是恢復方案。

恢復設計的必要不變式：

1. source owner 發布 link 後先讀取 fresh Monitor snapshot，保存舊／新 source revisions 與 authority digest。
2. Manager 以 expected run id、claim key、舊 digest、candidate head、PR number/head 和 delivery binding 做單次 CAS；任何前置值不同即停止，不自動重試或重寫歷史。
3. 舊 verify／review evidence、workflow job binding、delivery binding、merge authorization 分別依其 authority/input hash 定義有效性；未有明文等價證明即失效，需從 exact candidate 重做必要 gates。
4. 既存 PR 只作 GitHub 遠端事實採信，不因 resume 重送 push/create/merge。外部 outcome unknown 時停在 needs-human，由 operator 使用明確 evidence 裁決。

這些前提牽涉多個 durable owner 和不可逆 GitHub side effects；現有證據尚不足以證明同 run resume 或新 run adoption 哪個完整可行。因此 R4 不得由單一 admission PR 以假定取代實作研究，須拆出 recovery 子票，凍結可執行的 CAS／invalidation／no-duplicate protocol 後再進件。

### D5 — 相關 issue 的適用邊界

- #911 已修 ship 的 `mapped_openspec=0` 分支；#1051 不重做或回歸要求一筆 OpenSpec。
- #765 提供 authority 前進使 claim/job binding 失效的風險證據；它不是 Todo=0 的相同修復。
- #810 的問題是 merge 後 Todo checkbox 未完成而卡 remote closure；#1051 處理的是 build 前沒有任何已授權 Todo path。此處不放寬 checkbox 完成度。
- #983 的 delivery journal conditional-write work item不由 #1051 編輯；PR #1049 的 main conflict 維持 #972／#973 範圍。

## Projected five-dimension sizing

此為完整 #1051 scope 的 draft projection，採目前 issue classifier 的 `fix-standard` combo 與本 repo 的 sizing inputs；拆票後每個 child 必須各自重算。

| Dimension | Score | Basis |
|---|---:|---|
| `domain_breadth` | 2 | 需要 Manager admission、WorkAuthority／claim reconciliation、diagnostic 和 delivery/recovery owner 多處交界，預計超過三個 production modules。 |
| `state_consistency` | 2 | source revisions、claim/job binding、run evidence、delivery journal 與 GitHub PR facts 必須以 CAS 和 exact identity 協調。 |
| `acceptance_surfaces` | 2 | `fix-standard` gate spine 加上 R-09、R-16、R-19 process rules 超過 sizing 門檻。 |
| `spec_stability` | 2 | 此包刻意保持 draft；恢復路徑仍需要獨立 CAS／evidence invalidation 子票與 review 才能定案。 |
| `orchestration` | 2 | 多個 workflow card、Manager、Monitor/WorkAuthority owner 和外部 GitHub facts 交互。 |
| **Total** | **10 / Red** | 依 `planning.compute_sizing_score()` 的現行五維算法。 |

## Proposed issue-backed split

這是待 maintainer 依 R1–R5 與正式 sizing gate 建票的建議，不代表 child issue 已建立，也不授權進入 intake。

### Child A — build 前唯一 Todo admission 與零來源 diagnostic

- 建議標題：`fix(work): Superpowers-only work 在 builder dispatch 前要求唯一 Todo source`。
- 範圍：D2–D3 的 owner/mapping 規則與 build admission gate；對 zero/multiple mapping 分類並提供有效 next action；ship 保留 backstop。
- 驗收：#1051 AC1–2；測試 Superpowers-only／無 OpenSpec／唯一 Todo／缺 Todo／多 Todo／偽造 path link，證明缺少或歧義時 builder job 為零，正式 source link 尚未進 fresh snapshot 時不放行。
- 邊界：不處理既有 candidate／PR run 的 claim era rebase、delivery journal recovery 或 merge 重入。

### Child B — authority 前進後 exact Candidate／PR 的正式恢復

- 建議標題：`fix(recovery): Todo authority 加入後安全恢復已有 candidate 與 PR 的 run`。
- 範圍：#983 型 stopped run；在 WorkAuthority、claim/run、evidence、delivery journal 和 GitHub PR 之間定義同 run CAS 或新 run adoption 的唯一合法方式。
- 驗收：#1051 AC3 及 AC4 的「已存在 PR」情境；驗證 old/new revisions、claim CAS、證據失效／重驗範圍、exact candidate／PR head、無重複 push／PR／merge，並在 unknown/conflict 時明確 fail-closed。
- 邊界：不處理 PR #1049 的 CHANGELOG/main conflict（#972／#973），不手改正式 registry、journal 或 source revision。

## Risks

- `WorkAuthority` snapshot 更新是非同步的；draft diagnostic 必須分清 override 已寫入與 fresh authority 已確認。
- 來源在 plan review 後加入可能使 claim era 前進，沿用既有 run 並不自然成立。
- 若以 PR link 直接採信舊 evidence，可能把不同 authority 下的 review／verification 誤當有效。
- 將 plan checkbox 當 Todo 會讓規劃 authority 冒充交付完成 source，破壞 #810 與 current remote closure 的獨立責任。
