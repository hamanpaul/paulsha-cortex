### Fixed

- **#716 codex code-mode host**：codex 0.157 起 `features.code_mode_host`（stable、預設開）要求 `codex-code-mode-host` 與 codex 本體同目錄；qualification／canary 只安裝單檔 codex，job 內每個 tool call 都因 host 不存在而失敗。codex 改以 npm 平台套件的整個 `bin/` 目錄做 tree 安裝（`archive_dir`＋`entrypoint`），installer 對進入點不是 `.js`／`.mjs`／`.cjs` 的 tree 產生直接 exec 的 wrapper（不經 node）（#716）。
