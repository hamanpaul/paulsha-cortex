## Tasks

- [x] **T1 RED**：模擬 run 已有 PR、authority 前進、最後的 repair build 為 `failed`（exit 1、沒有 terminal），證明現行所有 operator 動作都被拒或不適用。
- [x] **T2 可受理的恢復動作**：`retry-build` 在 build phase 的 admission 也接受「最後一張 builder 卡的最新 job 是 terminal 失敗（failed／被終止），且沒有產生被採信的 evidence」，比照從 verify／review 打回時的重設：重新綁定 authority、留 receipt，派出新的 builder job。
- [x] **T3 next_actions 正確**：此狀態下 `next_actions` 列出 T2 的動作，不再列出必然失敗的 `resume`；若真的沒有合法動作，要明確標示為缺陷狀態。
- [x] **T4 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
