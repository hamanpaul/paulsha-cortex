# Tasks

- [x] 新增 canonical `.project-policy.yml` 缺少 `tier` 時的 RED regression test，要求診斷列出 manifest 路徑與允許值。
- [x] 前置驗證：foreign review 為 required 的流程，在 builder dispatch 前檢出缺失／非法 `tier`。
- [x] 診斷訊息在所有缺失／非法 `tier` 情境一致且可操作。
- [x] tier 前置驗證通過時不建立 `needs_human` 狀態，候選仍可進入 builder dispatch（archive 前置範圍）。
- [x] 測試涵蓋無 manifest、canonical 缺 `tier`、非法 `tier`、合法 `shareable`。
- [x] 文件化 canonical manifest 缺 `tier` 的 fail-closed 語意。
- [x] slice lane（`run_tick`／`retry-build`）在 builder dispatch 前共用 tier 前置驗證，避免晚期 foreign review 設定錯誤。
- [x] deck、doctor、delivery preflight 與 claim readiness 共用同一個 tier resolver，於各自權威前置面 fail-closed。
- [x] production claim、direct slice dispatch 與 workflow preflight 均實際接入 tier/readiness checkpoint；repo-root resolution error 保留原分類。
- [x] 文件同步涵蓋 canonical 與 legacy manifest 的缺 tier 語意；所有變更仍限於 archive 前置範圍。
