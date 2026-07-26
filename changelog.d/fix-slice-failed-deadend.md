### Fixed

- **Issue #153：支持 failed slice 恢復與 registry daemon 外部 jobs.json 重載**：`apply_slice_action` 現在可對 `failed` Slice 執行 `retry-build`，`registry` 會支援偵測 `jobs.json` 外部改動並重載，並保留恢復 action 供運維介面重入。
