---
status: accepted
work_item: dispatch-reliability-batch
---

# dispatch-reliability-batch Specification

#177 前置批 1：修正三個使 cortex 派工鏈不可靠的 open bug（#152、#100、#99），讓從 systemd 啟動的 manager daemon 能穩定派工、失敗時有可診斷的例外與時間戳、CLI 不再因 5s timeout 誤報失敗。範圍限定本機 pre-archive 可驗證的程式與測試變更。

## Requirements

### R1 mutation request timeout 依類型放大且語意明確（#152）

`coordinator/cli.py::_submit_mutation_request` 的 poll timeout MUST 不再是單一固定 5s 常數，而依 request 類型分級：

- `fanout`、`tick`：≥ 60s（建 worktree + pin inputs + headless launch 實測常 >5s）。
- `complete`、`work` 類 mutation：≥ 30s。
- 其他：維持 5s 預設。

timeout 發生時 MUST NOT 直接以「daemon 未在 N s 內完成」硬錯誤收場；MUST 輸出 request id 並明示「request 仍在 daemon 處理中，勿立即重試」，附查詢指令（`cortex request list` / `cortex status`）。exit code 須區分「已提交但未在 timeout 內完成」（可追蹤）與「真正失敗」。

### R2 DispatchReadyError 攜帶 per-slice 例外且寫入 tick response（#100）

`autonomy.dispatch_ready()` 收集的 per-slice 例外 MUST 進入 `DispatchReadyError` 的可讀訊息（含 slice id 與底層例外 type/message 摘要，如 `FileNotFoundError: <path>`），並 MUST 寫入 tick response 的 `errors` 欄位，使 operator 不必離線重演即可定位。`DispatchReadyError` 的既有 `jobs` 欄位（成功 jobs）MUST 保留。

### R3 manager.log 每行加 ISO-8601 時間戳（#100）

`manager_daemon` 的 log 輸出 MUST 每行前置 ISO-8601 時間戳（UTC，含毫秒），使新舊錯誤可區分；不破壞既有 log 格式 consumer（保持行尾內容不變，僅前置時間戳）。

### R4 git runner 消除 cwd 耦合（#99）

`dispatcher._default_git_runner` MUST 不依賴行程 cwd；MUST 以 `git -C <repo_root>` 執行，`<repo_root>` 取自 `paths.repo_root()`（`PSC_REPO_ROOT` 或 cwd 解析）。installer 模板產生的 `cortex-manager.service` MUST 含 `WorkingDirectory=<repo root>` drop-in 或 unit 欄位，使 systemd 啟動的 daemon 即使無 override 也能派工。兩者並行為 defense in depth。

### R5 限制

- stdlib-only；TDD（mock `subprocess.run`/`systemctl`/log capture fixtures）。
- 不得改變既有對外 CLI 命令的回傳 schema（`--json` envelope 頂層 schema 字串不變），僅擴充 timeout 路徑的訊息內容與 exit code 語意。
- `test_zero_dependency_runtime` 續綠；`python3 -m policy_check --repo .` 0 fail。
- 不處理 #175/#83/#158（屬批 2/3）。