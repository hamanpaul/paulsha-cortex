### Added
- **Refs #294：slice spec 可宣告 executor/model_id 並於派工前強制 registry 驗證**：`dispatch_ready` 支援逐 slice 的 builder identity 覆寫，unknown identity fail-closed 並列出可用 candidates；同時 `cortex fanout`／`tick` 的明確 `(executor, model)` 與 periodic tick 預設 model 也改為先查 `model-identities.yaml`，避免 typo 直到 session 內才失敗。
