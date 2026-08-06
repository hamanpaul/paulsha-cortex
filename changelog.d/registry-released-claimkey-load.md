### Fixed
- **Issue #302：registry 載入層 claim_key 唯一性改為只約束 ongoing runs**：
  `_load_state` 原對 claim_key 做全域唯一性 fail-closed，與 abandon→reclaim
  （#256 D4／#299）的「released row＋新 ongoing run 合法共用 claim_key」語意矛盾；
  重 claim persist 後 manager 重啟即無法載回狀態檔，且 builder worktree 內讀取
  production 狀態檔的測試全數 fail-closed。改為 ongoing runs 內唯一；run_id
  唯一性維持全域。
