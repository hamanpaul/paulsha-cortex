---
status: accepted
work_item: fix-git-runner-cwd
---

# fix-git-runner-cwd Design

## Decisions

- 採 `git -C repo_root` 根因修復 + installer `WorkingDirectory` 並行（defense in depth）：任一缺失仍可派工。
- 不改 `GitRunner` 簽名；相對路徑參數在 `-C repo_root` 下與「cwd==repo_root」行為一致。
- 既有已安裝 unit 不自動遷移，避免覆寫 operator override；`cortex service install` 重新 render 時帶入。
- 測試 monkeypatch `paths.repo_root` 回 `tmp_path` 並斷言 argv 含 `-C`。