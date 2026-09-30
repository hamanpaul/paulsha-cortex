### Fixed

- **#716 非 codex job 不 seed Codex 憑證**：launcher（降權模板 job）與 planning job 過去不論 executor 都 seed 並收回該 principal 的 Codex 憑證；部署沒有對應 Codex 憑證時（canary 的 reviewer 是 agy／copilot），verification 卡在 provision 當下就以 `credential authority is unavailable` 停住。seed 與收回改為只在 executor 是 codex 時進行，與既有 `credential_publish` 同一判準（#716）。
