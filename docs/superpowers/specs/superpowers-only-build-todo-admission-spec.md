---
status: accepted
work_item: superpowers-only-build-todo-admission
issue: 1054
---

# Superpowers-only 工作在 Builder 前的唯一 Todo admission 規格（#1054）

## Requirements

### R1 — 首次 Builder 派工只接受一筆已資格化的 canonical Todo

在尚無任何本 run Builder job 的情況下，Manager 在 plan→first-Builder 的實際派工點，必須用 strict WorkAuthority reader 取得當前 trusted Monitor generation，再消費唯一資格化的 canonical Todo source。來源數量恰為一才可繼續。0 筆產生 `missing-canonical-todo-source`；多筆產生 `ambiguous-canonical-todo-source`。WorkAuthority 缺席／歧義、Monitor 最新 attempt 失敗、correlation input revision 未反映目前 link override、snapshot 過期、generation 缺失／不一致、source qualification 不通過，皆 fail closed。

Stop 在任何首次 Builder side effect 之前：不得 reserve/create Builder job、選定後建立 Builder worktree、呼叫 `registry.create_job()` 或 `launcher.launch()`。只有唯一、合格、同代且 fresh 的 source 可通過。此門只保護首次 Builder；已存在 Candidate／PR 的 claim-era recovery 屬 #1055。

### R2 — Todo 資格由 Monitor source owner 提供

唯一可計數 source 必須是 owner-published 的 `docs/superpowers/workstreams/<slug>/todo.md`，其 `issue` provenance 對應同一 WorkAuthority 的 GitHub issue、`work_item` 完全匹配 authority work_id，且 `## Tasks` 有非空、非 placeholder、可執行項目。Owner-published 在本合約的可驗證含義是 canonical source 出現在受監控 repository，且 metadata 與 work-item/issue correlation 通過；不得宣稱 glob、checkbox 或 path 本身可證明 GitHub 帳號作者身分。

此資格由 #1063 的 Monitor/correlation source contract 提供。當前 `providers.py` glob/safe-file/revision、`_frontmatter_work_item_text()` 的單欄位抽取、correlation 的 `resolve(strict=False)` path guard 和 `_mutate_override()` 均不滿足全部條件，不能在本票中假設已驗證。Manager 只消費 #1063 輸出的 qualified source，不重寫 parser、不從 basename 或 accepted plan 推斷資格。spec/design/plan、checkbox 或手動 path override 本身均不可替代 Todo。

`cortex work link <work_id> --repo <owner/repo> --kind path --ref <repo-relative-todo-path>` 僅在 source 已存在、位於 repo 內、安全且屬 Monitor 支援的 scanner source 時得成功；一般 path link 可保留其他 scanner source 類型用途。此 existence/path guard 由 #1063 負責；本票的 Todo admission 另只計數符合 #1063 semantic Todo qualification 的 source。存在的 path 或 override 不代表 Todo 合格，也不等於 WorkAuthority 已更新。#1064/#1065 提供 latest successful correlation generation 對目前 override input 的證據；未觀察 link 變更的舊 snapshot 不可信。

### R3 — zero／multiple diagnostics 可操作且不會指錯修復

兩種拒絕使用穩定 typed `DiagnosticReason`，記錄 Manager source、run/work identity、映射數、可讀 authority reference（snapshot generation/hash 與 source revision 摘要）和 `next_step_hint`。

- zero reason 固定為 `missing-canonical-todo-source`。提示 owner 發布含匹配 issue/work_item/Tasks 的 canonical Todo，link 已存在且合格的 path，等待 trusted API 確認最新 successful generation 後再走正式 Manager admission。不得建議 unlink。
- multiple reason 固定為 `ambiguous-canonical-todo-source`。指出具體多餘 path 與目前數量，提供只解除多餘 mapping 的精確 `cortex work unlink <work_id> --repo <owner/repo> --kind path --ref <repo-relative-extra-path>` 建議，要求等待新成功 generation 再走正式 admission。

若 link 改變令目前 run 的 claim 過期，診斷不得叫操作者 resume 舊 run；見 R4。

### R4 — 在首次 Builder 前驗證 exact run/claim；過期舊 run fail closed

首次 Builder dispatch 及對仍未建立任何 Builder job 的直接 `resume`，都必須對 exact `run_id` 重讀 R1 的 trusted WorkAuthority。計算目前 authority 去除僅屬本 workflow planning outputs 的 digest（沿用 `authority_digest_without_planning_outputs()`），要求它逐位相等於持久化 `run.source_revision`；同時要求 `run.claim_key` 等於由同一 `run.repo`、`run.work_id`、`run.source_revision` 導出的 claim key。不得以「Todo/source revisions 在 plan 後可變」為理由忽略 Todo；該 helper 只剝除 `superpowers_spec:`／`superpowers_plan:` 自產物，Todo mapping/source revision 必須仍造成 digest drift。此比較不寫舊 run 的 claim、source revision、evidence 或 delivery state。

不相等時回傳獨立 typed `stale-pre-builder-claim` stop，包含 exact run、old/current digest 和 authority ref；不得從直接 resume 或 plan-final transition 進入 Builder dispatch。計數器必須證明無 Builder job reservation/creation、worktree 或 agent launch。gate 放在 `manager.py` plan→build transition 之後、首次 Builder side effect 之前，且 direct resume 走同一判斷；不能只靠 start/intake 的 claim 分支。

### R5 — 過期的 pre-Builder run 只能明確放棄後新建 generation

在 owner link 已被 latest successful Monitor generation 確認後，若舊 run 沒有任何 Builder job、Candidate、PR 或 active job，合法 continuation 是操作者明確執行既有 `cortex work abandon <work_id> --repo <owner/repo> --actor <operator> --expected-run-id <exact-run-id> --reason <single-line-reason>`，再用 formal Manager `start`/`intake` 建立新 generation。Abandon 是誠實放棄一個未交付的舊 run：必須 exact-run CAS，保留舊 claim／evidence 作稽核，不把舊 run 說成完成；其既有 planning-artifact GC 不得移除 owner Todo。不得用舊 `resume`、手改 registry/claim key、或重新認領舊 run 來跨過 digest drift。active job 時 fail closed 等待；任何 Builder job 已存在時本票不重設其 run；已有 Candidate／PR 走 #1055，不用 abandon 偷渡為成功或覆蓋 #1055 recovery。

### R6 — 保留 ship、OpenSpec 與周邊 issue 的所有權

`work_actions._ship_action()` PR=1、Todo=1、OpenSpec=0/1 backstop 必須保留。#911 支援的 OpenSpec=0 ship lane 不得被禁止。#810 的 merge 後 Todo checkbox closure、#972/#973 的 PR #1049 main conflict 各自獨立。#983 已有 Candidate／PR 的 claim/evidence/delivery recovery 由 #1055 負責；#1054 不操作既有 PR、verify/review evidence、delivery journal 或已建立 Builder run 的 claim generation。

### R7 — 真 Manager／Monitor／WorkAuthority boundary tests

使用真實 Manager plan→build dispatch seam、WorkAuthority reader、Monitor generation 與 source qualification fixtures。覆蓋 Superpowers-only / OpenSpec=0、唯一 Todo、zero、multiple、來源 metadata/path 不合格、link override 新寫未被 successful generation 觀察、舊成功 Todo row 加最新 refresh error、過期/缺漏 generation、exact run claim match、Todo/source drift、plan-output-only drift，以及 direct stale resume。每一 stop 都斷言 Builder job reservation/creation、Builder worktree、agent dispatch 均為零；唯一 fresh qualified source 且 exact claim match 才可通過。fixture 不連真 GitHub、不修改正式 Monitor snapshot，不操作正式 run 或 PR。

## Authority boundaries

- Manager 擁有首次 Builder admission、stale-pre-builder-claim typed stop 與 operator next action。
- #1063 的 Monitor/correlation qualification 是 issue provenance、matching work_item、Tasks 和 existing safe path 的 source fact owner；Manager 不推導它。
- #1064 提供 latest correlation refresh attempt/input-generation producer；#1065 的 WorkAuthority reader 消費該可信 freshness API；Manager 只使用其 result。
- Run claim 由持久化 `source_revision`／`claim_key` 表達；Manager 在第一次 Builder 前精確重讀比對，不重寫 claim。要繼續過期的 pre-Builder work，使用 exact-run abandon 加 formal new start/intake。
- #1055 是已有 Candidate／PR 的 recovery owner。#1053 是 Draft parent context，不是本票 authority。
- GitHub issue 提供 issue identity，不代表 Todo mapping 已存在；Superpowers planning artifact 也不是 delivery Todo。

## Acceptance

- 首次 Builder side effect 前只有一筆 qualified canonical Todo，且該 source 來自 trusted latest successful Monitor generation。
- zero、multiple、snapshot error/stale/input mismatch、source qualification failure、stale claim 都在 dispatch 前 typed fail closed，Builder job/worktree/agent 次數為零。
- exact claim reconciliation 忽略 workflow 自產 spec/plan revisions，但不得忽略 Todo/link/source drift；direct stale resume 不派工、不變更舊 claim。
- zero/multiple diagnostics 含真實數量、authority reference 和正確可執行 next action；需要新 claim 時明示 exact abandon + formal start/intake；multiple 才建議 unlink。
- existing ship backstop、OpenSpec=0、#810、#911、#972/#973 與 #1055/#983 recovery ownership 保持完整。
