### Fixed

- **#716 模板模式的 gate ledger 綁定 job**：降權（systemd 模板）模式下，Manager 從 gate spool 重建權威 ledger 時，`slice_id` 取的是 Manager 自己 env 的 `PSC_SLICE_ID`。服務行程沒有這個變數，所以會是空字串，canary 的結案檢查因此判 `workflow gate ledger schema is invalid`。現在由呼叫端明確帶入 job id，行為與 direct 模式的 wrapper 一致。測試的 base env 原本就帶著 `PSC_SLICE_ID`，這個缺陷因此一直被遮住（#716）。
