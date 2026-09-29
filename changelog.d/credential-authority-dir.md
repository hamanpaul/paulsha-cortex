### Fixed

- **#716 Codex 憑證目錄 owner**：scaffold 先以 Manager 身分建立 `codex-credentials/<principal>/`（0700，拒絕 symlink）再寫入 `auth.json`；原本 `install -D` 留下 root-owned 0755 目錄，三 UID 安裝的 Manager 在 job 結束收回 refresh 過的憑證時 EACCES，整條派工以 `runtime-contract-failed` 終局。既有部署重跑 scaffold 即修回（#716）。
