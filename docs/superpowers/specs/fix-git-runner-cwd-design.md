---
status: accepted
work_item: fix-git-runner-cwd
---

# fix-git-runner-cwd Design

## Decisions

### D1 git -C repo_root（根因修復）

`_default_git_runner` 改為 `subprocess.run(["git", "-C", str(paths.repo_root()), *args], capture_output=True, text=True)`。`paths.repo_root()` 在 daemon 下經 override `WorkingDirectory` 已是 repo root，改顯式 `-C` 後即使 override 缺失亦正確。失敗時沿用既有 `RuntimeError(f"git ... 失敗: {stderr}")` 訊息，前綴補 `-C <path>` 以利診斷。

### D2 installer WorkingDirectory（defense in depth）

installer 產 unit 時於 `cortex-manager.service` 模板加 `WorkingDirectory=<repo_root>`（render 時代入 `paths.repo_root()`）。不依賴 operator 手動 drop-in。monitor service unit 亦加同欄位（其掃描亦呼 git/gh）。既有已安裝 unit 不自動遷移（避免覆寫 operator override）；`cortex service install` 重新 render 時帶入。

### 風險與 mitigation

- 既有測試以 cwd-relative git 行為為假設：改 `-C` 後相對路徑 resolve 到 `paths.repo_root()`，與原 cwd==repo_root 一致；測試以 monkeypatch `paths.repo_root` 回 `tmp_path` 並斷言 argv。
- `paths.repo_root()` 在非 repo 目錄下解析為 cwd：daemon 已有 WorkingDirectory 保證；CLI 一次性命令在 repo checkout 內執行，cwd 即 repo root。可接受。