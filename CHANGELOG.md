# Changelog

本專案所有重大變更都會記錄在此檔案。

格式基於 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/)，
本專案遵循 hamanpaul project policy v1.0.7。

## [Unreleased]

### Fixed
- **`cortex reap-brokers` 失敗時改回 non-zero exit**：操作員手動執行 cleanup 時，若腳本缺失、無法 exec，或腳本以非零碼結束，CLI 仍會印出 JSON summary，但現在會回傳 exit 1，避免把未執行/失敗的 cleanup 誤報成成功。
- **service installer 會持久化 manager Python 解譯器**：`cortex install service` 現在會把 `PY=<sys.executable>` 寫入 `~/.agents/core/runtime/<instance>-manager.env`，避免 pipx / venv 搭配 user systemd 時落回系統 `python3` 而找不到 `paulsha_cortex` 模組。
- **service installer 會持久化正確 repo root**：`cortex install service` 新增 `--repo-root`，會先驗證目標是否為 git repo，再把解析後的 top-level 路徑寫入 `PSC_REPO_ROOT`，避免 manager daemon 在 systemd cwd 下把 worktree 建到錯誤目錄。
- **hook 模板改為透過 `cortex relay-hook` 定位封裝腳本**：三份 hook JSON 不再硬編不存在的 repo 內路徑，也移除了不屬於 cortex 的 `psc-bro-return` glue；`relay-hook` 子命令會直接執行封裝內的 `psc-relay-hook.sh`，安裝位置改變時仍可正確解析。
- **停止 periodic automatic reaper，改為 scoped operator cleanup**：`tick` 與 manager daemon 不再自動回收 codex broker；新增 `cortex reap-brokers` dry-run/operator 路徑，`--apply` 必須搭配 `--cwd-root`，腳本會在送 `SIGTERM` 前重驗 `ppid/start-time/cmdline/cwd`，只清理同 project scope 內、身份未變的 broker。

### Changed
- **同步 policy 1.0.6 → 1.0.7（R-24 moc-alignment）**：`policy_version` 1.0.6 → 1.0.7；`Policy Check` workflow re-pin 引擎到 1.0.7 SHA `e24fbd6`（尾註 `# v1.0.7` 供 R-23 對齊）、`policy_version` / `policy_engine_ref` 同步；CLAUDE.md 補 v1.0.7 新增規則段（R-24）與白名單 `policy-exempt:moc-alignment`。
- **採用 policy 1.0.6 新模型（agent 慣例檔 symlink 單一真檔 + 引擎 pin attestation）**：`AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md` 改為指向 canonical `CLAUDE.md` 的 symlink；`.paul-project.yml` 設 `agent_files.mode: symlink` 與 `conventions_engine.repo`，`policy_version` 1.0.2 → 1.0.6；`Policy Check` workflow re-pin 引擎到 1.0.6 SHA `261f3f6`（尾註 `# v1.0.6` 供 R-23 對齊）、`policy_version` / `policy_engine_ref` 同步；CLAUDE.md 補 v1.0.3–v1.0.6 新增規則段。修正 P0 傳播漂移（本 template 先前停在 1.0.2）。

### Added
- **新增 `tests.yml` CI 骨架**：生成的新 repo 出生即帶測試 gate——`tests/` 尚不存在時 job 自動跳過（綠燈），加入測試套件後 pytest 自動成為 PR gate，同時滿足 policy R-19 的 workflow 偵測
- 建立 `hamanpaul/new-project-template` 新專案 bootstrap skeleton
- 新增釘選到 `hamanpaul/paulsha-conventions` 的 `Policy Check` reusable workflow
- 新增同步的 agent convention files 與基本 policy metadata

### Changed
- **同步 policy 1.0.2**：bump `policy_version` 1.0.1 → 1.0.2（`.paul-project.yml` 與四份 agent convention files、`managed-by@v1.0.2`），caller `Policy Check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@98487868a098e22647074c677a58633ce4fa19be`（= engine tag `v1.0.2`，含 R-19 / R-20）；agent 檔追加 R-19（CI 必須跑測試）/ R-20（workflow policy_version 同步）說明與 `policy-exempt:ci-tests` 白名單項
- **同步 policy 1.0.1**：bump `policy_version` 1.0.0 → 1.0.1（`.paul-project.yml` 與四份 agent convention files、`managed-by@v1.0.1`），caller `Policy Check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@4ff59b6c35a46a87af3c3e641975743ee8fa0858`（含 R-17 / R-18）；agent 檔追加 R-17（PR↔issue closing-keyword）、R-18（docs 對齊 WARN）與語言規範說明
- `Policy Check` workflow 改為雙重釘選 `hamanpaul/paulsha-conventions@8454aa1967b752ea38c82edd79a8439b5bde915b`，同步設定 reusable workflow `uses:` 與 `policy_engine_ref`

### Fixed
- 移除超出需求範圍的 `pyproject.toml` 與相關 package 化敘述
