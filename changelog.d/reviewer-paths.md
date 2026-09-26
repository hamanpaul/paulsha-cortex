### Fixed

- **#571、#579 reviewer 路徑綁定**：review gate evaluation 檔名納入 candidate 短 SHA，避免重用 reviewer job id 時覆蓋不同候選的 immutable evidence；reviewer sandbox 目錄名納入 job id，並在新 reviewer 派工前回收前代 claim era 已終止的孤兒 sandbox。
