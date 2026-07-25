---
status: accepted
work_item: dispatch-reliability-batch
---

# dispatch-reliability-batch Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_dispatch_reliability.py`：
  - #152：`_submit_mutation_request` 對 `fanout`/`tick` 使用 ≥60s timeout、`complete`/`work` ≥30s、其他 5s（mock `poll_done_fn` 控制逾時）；逾時路徑回傳 pending 結果含 req_id 與追蹤指引訊息、exit code 區別於失敗；成功路徑不變。
  - #100：`DispatchReadyError.__str__` 含 per-slice `type: message` 摘要；tick handler 把 `errors` 寫入 response `errors`；`jobs` 保留。
  - #100：manager.log 每行首欄為可解析 ISO-8601（log capture fixture）。
  - #99：`_default_git_runner` 以 `git -C <repo_root>` 呼叫（mock `subprocess.run` 斷言 argv 含 `-C` 與 `paths.repo_root()`）；installer 模板 render 含 `WorkingDirectory=`。
  - 先確認 RED。

### 2. 實作

- [ ] `paulsha_cortex/coordinator/cli.py`：`_REQUEST_TIMEOUTS` 表 + `_submit_mutation_request` 依 req_type 取 timeout；逾時 pending 路徑 + `EXIT_SUBMITTED_PENDING` 常數。
- [ ] `paulsha_cortex/coordinator/autonomy.py`：`DispatchReadyError.__str__` 組 per-slice 摘要（cap message 長度）。
- [ ] `paulsha_cortex/coordinator/manager_daemon.py`：tick handler 把 `DispatchReadyError.errors` 寫入 response `errors`；log 寫入加 ISO-8601 前綴 helper。
- [ ] `paulsha_cortex/coordinator/dispatcher.py`：`_default_git_runner` 改 `git -C paths.repo_root()`。
- [ ] installer 模板：`cortex-manager.service` render 加 `WorkingDirectory=<repo_root>`。

### 3. 同步與驗證

- [ ] `changelog.d/dispatch-reliability.md` fragment；`CHANGELOG.md [Unreleased]` `### Fixed` 加入含 `dispatch-reliability` 字樣條目（涵蓋 #152/#100/#99）。
- [ ] README 對應段落同步（R-18 docs 對齊：CLI timeout 語意、manager.log 時間戳、git runner cwd）。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。
- [ ] 完成後勾選 `openspec/changes/2026-07-25-dispatch-reliability/tasks.md` 對應項並以 conventional commit 提交（不得改動本 plan 檔）。