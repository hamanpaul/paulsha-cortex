---
status: accepted
work_item: retry-build-killed-repair
---

# repair build 被外力終止後，PR 已存在的 run 要有可受理的恢復動作

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1338`（含完整的動作／結果表）。
- 現況：PR 已存在的 run，repair build 被 daemon 重啟終止（`failed`、exit 1、沒有 terminal evidence）之後，每一條恢復路徑都被拒：
  - `resume`、`retry-card`：WorkAuthority 已因 PR 變動前進，回 `builder-todo-authority-changed`。
  - `retry-build`：要求最後一個 builder job 是 exited 0、尚未綁定 evidence。
  - `rechain`：已有 PR，拒絕。
  - `recover-repair-commit`、`recover-pre-candidate`：不適用。
  - `abandon`：已越過 pre-delivery。
- 現場：`task-memory-disposition`（PR #1332），2026-10-07 02:12Z 升 pin 重啟後卡死。
- 不放寬 authority 綁定的既有安全性：重新綁定必須留下 receipt，並沿用 retry-build 的 exact-candidate 檢查。

## Tasks

- [ ] **T1 RED**：模擬 run 已有 PR、authority 前進、最後的 repair build 為 `failed`（exit 1、沒有 terminal），證明現行所有 operator 動作都被拒或不適用。
- [ ] **T2 可受理的恢復動作**：`retry-build` 在 build phase 的 admission 也接受「最後一張 builder 卡的最新 job 是 terminal 失敗（failed／被終止），且沒有產生被採信的 evidence」，比照從 verify／review 打回時的重設：重新綁定 authority、留 receipt，派出新的 builder job。
- [ ] **T3 next_actions 正確**：此狀態下 `next_actions` 列出 T2 的動作，不再列出必然失敗的 `resume`；若真的沒有合法動作，要明確標示為缺陷狀態。
- [ ] **T4 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
