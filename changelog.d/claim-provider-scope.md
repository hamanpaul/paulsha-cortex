# claim-provider-scope

- **`#530`：claim 的 GitHub provider 檢查改為 scope 到 work item 實際依賴的 provider**——
  `coordinator/claim.py:_authority_from_canonical_row` 原本無條件要求 `github:<repo>` 為 `ok`，
  完全不看該 work item 的 confirmed sources 由誰供應。於是 GitHub REST 不可用時，**連權威
  全部來自本機檔案系統 provider（`repo:<owner>/<name>`）的 work item 也無法 claim**——實測
  2026-08-14 帳號遭 abuse-detection 封鎖期間，只掛一份 workstream `todo.md` 的
  `fix-instance-config-isolation` 被 `provider-authority-rate-limited-canonical` 擋死，
  而它要讀的 todo 就在磁碟上、內容從未變動。一次 GitHub 可用性事故因此放大成整個 fleet 的
  派工全面停擺，包括「修 GitHub 壓力問題」本身，形成
  「限流 → 無法派工 → 修不了限流」的死結。
- fail-closed 對**確實掛著 GitHub source** 的 work item 仍然必要（provider 過時代表可能去做
  一張已被關閉、或 label 已被移除的 issue），故本次只收斂適用範圍，不放寬強度：
  - 新增 `WorkAuthority.requires_github_authority`（預設 **True**，保守）；由 confirmed
    sources 的 `provider` 前綴判定，`github:` 與 `github-terminal:` 皆屬 GitHub 來源，
    `kind in {github_issue, github_pr}` 為第二道保險（remote todo／openspec 的 kind 看不出來源）。
  - **資訊缺席一律保守**：`provider` 欄位缺失或非字串時視為可能來自 GitHub，維持既有嚴格
    fail-closed；放寬只發生在 source 明確標示了非 GitHub provider 時——亦即有正面證據，
    而非「沒看到證據」。
  - 豁免仍要求 last-known-good（`revision` 與 `last_success_at` 齊全）；缺 revision／壞
    timestamp 屬真正的 authority 損毀，照常拒絕。
- 同步修正**第二層放大**：`_authority_is_fresh` 以 GitHub 的 last-success 當時鐘判定過期，
  對不依賴 GitHub 的 authority 是錯配（其內容新鮮度已由 `source_revisions` 承載）。只修
  provider 檢查而不修這裡，claim 仍會在 `decide_manual_start`／auto-claim 被擋下。
- 第三層放大（`reduce_lifecycle` 的 `provider_degraded_freeze` 與 `hard_gates.auto_claim`）
  記錄在 `docs/superpowers/workstreams/fix-claim-provider-scope/todo.md` 待辦，本 PR 不動。
