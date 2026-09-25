---
status: accepted
work_item: openspec-remote-archive-authority-reconciliation
---

# OpenSpec local-active／GitHub-archived authority 衝突收斂規格（#961）

## Authority 與父子票分工

本文件對應 [#961](https://github.com/hamanpaul/paulsha-cortex/issues/961)，以 live issue #961 與父票 [#887](https://github.com/hamanpaul/paulsha-cortex/issues/887) 為需求 authority。#961 是 #887 的 Child A，承接父票 R5、R7、R8(e)–(f)：只修正已確認 OpenSpec authority source 的特定 active／archived 衝突。父票 R1–R8 全部保留，由 #961 與 [#962](https://github.com/hamanpaul/paulsha-cortex/issues/962) 分工完成；本票不得縮減父票的 aggregate acceptance。

執行順序：#961 先合併；#962 blocked by #961，待本票的 authority reconciliation landed 後才整合其 merged-run completion recovery。#887 只有在兩 child 均合併且父票完整整合驗收通過後才可完成。

## 背景與現況

#887 記錄的 run workflow-ced42c7b999df8bc222d 在 PR #884 merge 後，同一 OpenSpec change 被 canonical snapshot 以兩個 confirmed source 呈現：本機 RepoWorkProvider 的 repo:{repo} 回報 active；GitHub terminal snapshot 的 github-terminal:{repo} 回報 archived。現有 semantic_source_revision 將兩者映為 openspec:{repo}:{ref} 同一 semantic key，_authority_from_canonical_row 在第一個不同 value 出現時立刻拋出 source_revisions conflict，使已 merged run 無法載入 WorkAuthority。

GitHub issues provider 對 merged PR 可回報 closed；closed 本身不能證明 merge。可用的正面證據是 GitHub terminal snapshot 中對同一 confirmed github_pr source_id 的 remote_prs row，且 merged_with_merge_commit is True；該 terminal provider snapshot 也必須 status == "ok"。

2026-09-23 對 repo main @ 3e23f9dc4fcd2c0b19d9a46ffc13d4540e681d4d 的唯讀核對確認：
- claim.py:160-169 將 kind=openspec 的 source_id 正規化為 openspec:{repo}:{ref}，value 包含 identity/ref 與 active/archived 狀態；claim.py:735-762 目前用 setdefault 逐筆累積，第一個 value 不同即以原 row-malformed/source_revisions 欄位失敗。
- monitor/providers.py:221-229 的本機 OpenSpec source 使用 provider repo:{repo}、status active；:1715-1731 的 GitHub 樹 source 使用 provider github-terminal:{repo} 並保留 active/archived status。
- monitor/providers.py:1733-1792 建立 remote_prs，並以 github_pr:{repo}#{number} 作 source_id；:1808-1812 只有在 merge commit 可驗 ancestry 後才把 merged_with_merge_commit 設為 true。
- 目前 tests 中沒有直接涵蓋這個 canonical authority revisions conflict 的測試；monitor regression fixtures 已有相符的 remote OpenSpec 與 remote_prs 資料形狀可參照。以上是需求基線，不宣稱本票已修改或驗證 runtime。

## Requirements

### R961.1 保留既有 source membership 與 semantic mapping

先沿用 canonical row 的 confirmed-source 過濾、source 欄位驗證，以及 semantic_source_revision 的既有輸出。對一般 source、相同值的 OpenSpec sources 和既有 malformed source，結果、排序及錯誤語意保持不變。不得把 provider locator、PR timestamp、OpenSpec content hash或其他 provenance提升為新的 claim authority。

### R961.2 僅收斂一種精確衝突

只有同一 key 恰為 openspec:{repo}:{ref}、且其不同 values 恰為以下兩個，才可嘗試 arbitration：

- identity:{ref};state:active
- identity:{ref};state:archived

必須同時滿足下列條件，才選擇 archived value：

1. 所有貢獻 archived value 的 confirmed source，其 provider 均為 github-terminal:{repo}。
2. 所有貢獻 active value 的 confirmed source，其 provider 均為 repo:{repo}。
3. confirmed github_pr sources 非空，且每筆 status 都是 closed 或 merged。closed 是必要 lifecycle 形狀之一，但不能單獨當作 merge proof。
4. providers 中 github-terminal:{repo} 對應的 provider row 為 object、status == "ok"，observations 為 object，remote_prs 為 list。
5. remote_prs 對每筆 confirmed github_pr source 都必須恰有一筆 source_id 完全相同、merged_with_merge_commit is True 的 row。必須做精確 source_id 關聯；缺列、重複列或不同 PR 的 merge row 都不得替代此證據。

任何條件缺席、型別不符或真假值不符均視為沒有正面證據。此規則不得套用到其他 semantic key、其他值組合、三個以上 values，或 provider 角色顛倒的情況。

### R961.3 收斂結果與移除本機重複來源等價

符合 R961.2 時，該 key 的 semantic revision 固定為 identity:{ref};state:archived。整體 WorkAuthority 的 source_revisions、work_authority_digest 與 mapped_openspec 必須與同一 snapshot 移除本機 active source 後重新載入的結果完全相同。

相同 ref 的 mapped_openspec／changes 仍沿用既有 sorted-set 去重。Confirmed source 輸入列的任意排列不得改變選擇值、source_revisions 或 digest。

### R961.4 記錄可稽核且不洩漏路徑的 warning

每個成功收斂的 semantic key 記錄一行 warning，至少包含經安全過濾的 repo、work_id、change diagnostic label。change label 必須使用 _diagnostic_label；不得在 log 中帶出 workspace 路徑或原始檔案路徑。每個 key 只記一行，不因多個 confirmed source 重複記錄。

### R961.5 其他衝突原樣 fail-closed

R961.2 任一條件不成立時，保留既有 AuthorityValidationError base message、reason_code 與 field：
AuthorityValidationError("confirmed semantic work authority revisions conflict", reason_code="row-malformed", field="source_revisions")。

至少覆蓋：confirmed PR source 為空；PR 尚未 merged；remote_prs 缺席、非 list、無同 source_id row，或同 source_id 有重複且真假衝突／皆 true 的列；matched row 的 merged_with_merge_commit 不是 literal true；terminal provider 缺席或 status 非 ok；observations 型別錯誤；provider 組合顛倒；第三個 semantic value；以及非 OpenSpec key 衝突。不得用降級 warning、忽略 local source 或全域 archive precedence 放行。

### R961.6 測試以公開 authority load 結果驗收

新增 tests/test_post_merge_authority_restart_guard.py 的 Child A 測試，主要經 load_work_authority／canonical snapshot public boundary 驗證結果；純 helper 測試可補充，但不得取代 public-boundary 驗收。測試 fixture 以變數建立 change/ref 與 provider records，不寫死 archived change 的檔案路徑。

- 正例：嚴格來源條件成立時選 archived，WorkAuthority digest、source_revisions、mapped_openspec 與移除本機 source 後相同。
- 排序：交換全部 confirmed source 的順序，正例結果與 warning 次數一致。
- 稽核：成功只輸出一行 warning，含安全 label 且無路徑。
- 負例：逐條否定 R961.2 條件及 R961.5 列出的其他 key/value 情況，保留相同錯誤語意。
- 回歸：既有 load_work_authority、provider scope 及 monitor source fixtures 原斷言不變。

## Boundary

Production touchpoint 僅為 paulsha_cortex/coordinator/claim.py 的 semantic source 收集與 arbitration，以及必要的 module logger。新增測試放在 tests/test_post_merge_authority_restart_guard.py，與 #962 的測試擴充順序以 #961 先落地避免同檔衝突。

依 repo code-change policy，後續實作另需同步 changelog.d/openspec-remote-archive-authority-reconciliation.md、CHANGELOG.md [Unreleased]，並於 docs/unified-work-lifecycle.md 說明這項精確來源裁決與其 fail-closed 邊界；本次 accepted planning artifact 不改 repo 這些檔案。

## 父票完整 AC 對照

| #887 AC | 要求 | Owner |
|---|---|---|
| R1 | 新 mismatch 且已 manager-authorized merged 的 review run 跳過 authority restart，交既有 closure | #962 |
| R2 | claim key 已同步、已 reset 到 verify 的舊 run 有強證據 completion recovery | #962 |
| R3 | 新舊 run 的 journal/auth 不完整時 fail-closed，不假造 completion | #962；本票另保留 source-conflict fail-closed |
| R4 | 共用 phase-free journal helper，原 phase contract 不變 | #962 |
| R5 | 只有 exact remote merge proof 可將 local active／GitHub archived authority 收斂為 archived | #961 |
| R6 | 重驗 Manager authorization、trusted evidence、remote closure，冪等產生 completion | #962 |
| R7 | R5 以外衝突維持原 row-malformed/source_revisions 錯誤 | #961 |
| R8(a)–(d) | 新舊 run admission、evidence replay、fail-closed 與 registry completion tests | #962 |
| R8(e)–(f) | active/archived strict arbitration、來源順序與其他衝突 fail-closed tests | #961 |

#887 的完整 AC 仍是 aggregate outcome；此表只記錄子票 ownership，不把未由 #961 實作的項目視為刪除或完成。

## Non-goals

- 不改 GitHub/provider 的來源資料或 PR lifecycle status，不把 closed 當 merge proof。
- 不放寬其他 authority conflict，不修成任意 local-vs-remote precedence。
- 不寫 durable registry、delivery journal、outcome 或 WorkRun；不負責 run completion。
- 不改 manager.py、work_actions.py、registry.py、work_bridge.py、providers.py 或 work_api.py 的 runtime 行為。
- 不實作 #962 的 merged-run completion recovery；也不取代 #882 的一般 verify operator 出口或 #885 的 archive-applied 不變量。
- 不處理 #887 明確排除的 #847 merge 前 authority-restart、merge-authorized crash window，或 monitor completion_record_valid follow-up。
- 不新增 CLI，不新增 persisted 欄位或 schema。
