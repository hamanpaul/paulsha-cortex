---
status: accepted
work_item: archive-ref-namespace
---

# abandon 的保存參考不放 refs/tags；版本檢查只看發布 tag

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1314`，含 issue 留言（job clone 會複製本機 tag）。
- 現況：`coordinator/work_actions.py` 的 build branch reclaim，在分支有不在 base 的 commit 時，建立 `refs/tags/archive/<work>-<sha8>`。2026-10-06 這個 tag 剛好落在 main 的歷史上，造成三件事：
  - `git describe --tags --abbrev=0` 回傳它。
  - policy_check R-07 判定版本不符，所有 run 的 ship preflight 都失敗。
  - job 工作目錄是獨立的 clone，會複製 operator repo 的本機 tag，問題會擴散到每個 job。
- `trust_root/install/backend.py` 對 system 部署來源樹允許 `refs/tags/archive/*` 這個形狀。調整時要同步處理，或保留相容。

## Tasks

- [ ] **T1 RED**：測試 abandon reclaim 建立的保存參考會讓 `git describe --tags` 受影響（現行行為）。
- [ ] **T2 改用非 tag 命名空間**：保存參考改用 `refs/archive/<work>-<sha8>`（或其他非 tag 命名空間），保留「commit 可達」的目的；同步更新 trust_root backend 允許的 ref 形狀，並說明既有 `refs/tags/archive/*` 的相容或遷移方式。
- [ ] **T3 job clone 不複製非發布 tag**：建立 job 工作目錄時，不複製非 `v*` 的本機 tag；或 cortex 自己的版本檢查一律只看 `v*`（`--match 'v*'`）。
- [ ] **T4 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
