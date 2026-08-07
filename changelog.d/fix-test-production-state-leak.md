### Fixed
- **Issue #303：三個測試直讀 production coordinator 狀態檔，環境洩漏使本地 pytest gate 被宿主狀態污染**：`test_porcelain_inspect.py::test_inspect_missing_targets_exit_one[argv0-missing-job]`／
  `test_work_actions.py::test_auto_without_issue_mutates_every_mapped_issue`／
  `test_auto_without_issue_fails_closed_if_any_label_mutation_fails` 未隔離 coordinator root，
  在未顯式覆寫 `PSC_*` 的情況下經 `resolve_runtime_root()` 落回 `$HOME/.agents`，
  直讀（`JobRegistry`／`work_api` 間接經由 `_run_state_path()`）宿主真實
  `~/.agents/coordinator/jobs.json`；production 狀態異常時三測試連帶 fail-closed。
  `tests/conftest.py` 既有的 autouse `_clear_runtime_env` 只清空 `PSC_*`，未提供替代根目錄，
  等同讓解析退回宿主 `$HOME`——清得越乾淨、洩漏得越徹底。
- **同根因擴大排查：`tests/conftest.py` 新增 fail-safe autouse 安全網**：`_clear_runtime_env`
  現同時把 `PSC_AGENTS_ROOT`／`PSC_CONFIG_ROOT` 指向每測試獨立的空 tmp 目錄
  （`config/runtime.py` 的 `RUNTIME_ROOT_DEFAULTS` 讓 coordinator／control／specs／monitor／
  project-config／run root 全隨 `PSC_AGENTS_ROOT` 一併隔離），任何忘記自行隔離的測試預設即拿不到
  宿主狀態，而非逐一補丁；刻意不動 `HOME`，避免影響刻意驗證「host 環境即真實宿主」語意的
  `test_dispatch_runtime_preflight.py::test_preflight_uses_executor_environment_not_host` 一類測試。
  `test_paths.py`／`test_install_service.py`／`test_coordinator_manager_daemon.py` 5 支既有測試
  依設計顯式測試「未覆寫時真的落回 `$HOME`」的 fallback 語意，補上對應的
  `monkeypatch.delenv("PSC_AGENTS_ROOT")` 以在安全網之上保留原意圖（皆已改用各自的
  tmp `HOME`，不觸及宿主真實路徑）。
  以 `sys.addaudithook` 稽核全套件 `open`/`os.open`，證實修復前對命名測試的宿主
  `~/.agents/{coordinator/jobs.json,core/runtime/cortex-manager.env,config/paulsha/*.yaml}`
  皆有實際讀取；修復後歸零。以偽造帶重複 `claim_key` 的 corrupted `jobs.json`
  重現 W1 batch 描述的 fail-closed 情境：舊碼在該情境下三測試全部 fail，新碼因不再觸及該路徑而全數
  pass。
