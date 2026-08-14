---
status: accepted
work_item: fix-claim-provider-scope
---

# fix-claim-provider-scope Todo

`#530`：`claim.py` 的 GitHub provider 檢查是 **repo 範圍且無條件**——不看該 work item
實際依賴哪些 provider。於是 GitHub 不可用時，連權威完全來自本機檔案系統
（`repo:<owner>/<name>` provider）的 work item 也無法 claim，一次可用性事故被放大成
整個 fleet 的派工全面停擺，包括「修 GitHub 壓力問題」本身。

## 已完成（本 work item 的第一階段）

- [x] `_authority_from_canonical_row`：以 confirmed sources 的 `provider` 前綴（含
      `github-terminal:`）與 `kind` 判定 `requires_github_authority`；為 False 且
      last-known-good 齊全時豁免 `status != ok` 的擋阻
- [x] `WorkAuthority.requires_github_authority` 欄位（預設 True，保守）
- [x] `_authority_is_fresh`：不依賴 GitHub 的 authority 不受 `PROVIDER_MAX_AGE_SECONDS`
      新鮮度擋阻（三層放大的第二層）
- [x] 資訊缺席保守處理：`provider` 欄位缺失時維持嚴格 fail-closed
- [x] 測試涵蓋純本機來源可 claim、GitHub 來源仍 fail-closed、`github-terminal:` 來源仍
      fail-closed、缺 `provider` 保守、缺 last-known-good 仍拒絕

## 待辦（第三層放大與後續）

- [ ] `monitor/lifecycle.py:reduce_lifecycle` 的 `provider_degraded_freeze` 同樣 scope 到
      相關 provider——目前它排在規則序最前面，GitHub 一 degraded 就把**所有** work item
      凍在 `topic`，`cortex work show` 因此看不到可行動作
- [ ] `hard_gates.auto_claim` 以 `github:<repo> stale` 為條件關閉，同一問題的第三個面向
- [ ] 有 GitHub 來源的 work item 是否也該接受「有界過期」的 last-known-good（比照
      `#370` follow-up 的退休語境豁免），並在 claim 結果標示 authority 新鮮度，讓 ship／
      merge 等真正需要即時狀態的關卡自行再驗——需裁決
- [ ] 文件化：`docs/unified-work-lifecycle.md` 說明「哪些 provider 會擋哪些 work item」
