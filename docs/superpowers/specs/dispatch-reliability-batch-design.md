---
status: accepted
work_item: dispatch-reliability-batch
---

# dispatch-reliability-batch Design

## Decisions

### D1 per-request timeout 表 + 非阻塞 timeout 語意（#152）

在 `coordinator/cli.py` 引入 `_REQUEST_TIMEOUTS: dict[str, float]`，key 為 req_type（`fanout`/`tick`/`complete`/`work`/`run`），預設 5s。`_submit_mutation_request` 依 req_type 查表取 timeout，缺省回退預設。

timeout 路徑不再 raise 終止訊息為硬錯誤：改成回傳一個「submitted-but-not-done」結果物件，CLI 層印出 req_id + 追蹤指引，exit code 用新常數（如 `EXIT_SUBMITTED_PENDING`，區別於 `EXIT_FAILURE`）。保留既有成功/失敗路徑不變。此為最小變更，不引入完整 async submit 語意（避免擴大 scope）。

不選「改為 submit 後只回 req_id」全非同步：現有 operator 慣例是 `--wait` 同步等，全改會破壞 quickstart `cortex run tick --wait` 契約。採分級 timeout + 明確 pending 訊息即可消除「誤以為失敗→重試→撞 worktree already exists」連鎖。

### D2 DispatchReadyError 訊息與 tick response 透傳（#100）

`autonomy.py` `DispatchReadyError.__init__` 已收 `errors`（dict slice→Exception）與 `jobs`。`__str__` 改為組裝 per-slice 摘要：`f"dispatch_ready failed for slice(s): {', '.join(ids)}; details: " + "; ".join(f"{sid}: {type(e).__name__}: {e}" ...)``。tick response 組裝處（manager_daemon 的 tick handler）把 `DispatchReadyError.errors` 轉成 list[dict] 寫入 response `errors`。`jobs` 保留不變。

不把底層 traceback 整段塞入（cap 每則 message 長度，避免 flood）；完整 traceback 仍由 manager.log 逐行記錄（見 D3）。

### D3 manager.log ISO-8601 前綴（#100）

`manager_daemon` 的 log 寫入包一層 `_ts_log(line)` helper，前置 `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ ")`。既有逐行內容不變，僅前綴，故既有 grep/parse 不破。測試以 log capture fixture 驗證首欄為可解析 ISO-8601。

### D4 git -C repo_root + installer WorkingDirectory（#99）

`dispatcher._default_git_runner` 改為 `subprocess.run(["git", "-C", str(paths.repo_root()), *args], ...)`。`paths.repo_root()` 在 daemon 下經 override `WorkingDirectory` 已是 repo root，但改顯式 `-C` 後即使 override 缺失亦正確（root cause 修復）。

installer 產 unit 時，於 `cortex-manager.service` 模板加 `WorkingDirectory=<repo_root>`（render 時代入）。兩者並行：daemon cwd 正確 + git 顯式 -C，任一缺失仍可運作。

風險：部分既有測試以 cwd-relative git 行為為假設。改 `-C` 後，相對路徑參數 resolve 到 repo_root，與原 cwd==repo_root 行為一致；測試 fixture 需確認 `paths.repo_root()` 在測試環境指向 tmp repo。mitigation：測試以 monkeypatch `paths.repo_root` 回 tmp_path。

## 不做

- 不重構 fanout 為全非同步（D1 已述）。
- 不動 #175 delivery journal、#83 GC、#158 archived spec Purpose。
- 不新增對外 CLI 子命令。