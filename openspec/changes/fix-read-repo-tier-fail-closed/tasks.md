# Tasks

- [x] 新增 canonical `.project-policy.yml` 缺少 `tier` 時的 RED regression test，要求診斷列出 manifest 路徑與允許值。
- [ ] 前置驗證：foreign review 為 required 的流程，在 builder dispatch 前檢出缺失／非法 `tier`。
- [ ] 診斷訊息在所有缺失／非法 `tier` 情境一致且可操作。
- [ ] 已驗證候選不得僅因 tier 前置驗證缺失而落入 `needs_human`。
- [ ] 測試涵蓋無 manifest、canonical 缺 `tier`、非法 `tier`、合法 `shareable`。
- [ ] 文件化 canonical manifest 缺 `tier` 的 fail-closed 語意。
