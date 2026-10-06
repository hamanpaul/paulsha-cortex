---
status: accepted
work_item: ship-clean-behind-autosync
---

# ship 遇到 clean-behind 時自動同步 main，只重跑 verify 與 ship probe

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1311`。
- 現況：`paulsha_cortex/coordinator/work_bridge.py` 的 `_main_sync_stop_result` 把 main-sync probe 的 relation
  `clean-behind`（落後 main、沒有衝突）轉成 needs_human `candidate-behind-main`，`next_actions` 只有
  `retry-build`／`abandon`。之後要整輪重跑 build、verify、review，一輪約一小時。
- 2026-10-06 dogfood 現場：`copilot-probe-reap`（#1297，PR #1301）跑了三輪 build → verify → review → ship，
  每次到 ship 都落後 main；`upgrade-venv-umask`（#1295）、`system-deploy-ops-defects`（#1291）、
  `installer-launch-authorities`（#1289）也都遇到，每次都要 operator 手動送 `retry-build`。
- exact-candidate 純度不變：merge 的仍必須是 verify 過的那個 exact Candidate。
- 有衝突（`candidate-conflicts-with-main`）或 probe 失敗時，維持現行行為。

## Tasks

- [ ] **T1 RED**：新增測試，固定以下行為（現行應失敗）：
      - relation 為 `clean-behind` 時，Manager 自動把當下的 exact main 合進候選，產生新的 Candidate，並寫入
        main-sync evidence。
      - 只重跑 verify 與 ship probe，不重派 build 與 code-review，run 不設 needs_human。
      - relation 為 `candidate-conflicts-with-main` 或 probe 失敗時，行為與現行完全相同。
- [ ] **T2 自動同步**：在 `_main_sync_stop_result` 的呼叫端實作 clean-behind 自動同步。合併由 Manager 以受控方式
      完成，不交給 model；合併失敗（例如同步當下 main 又前進並產生衝突）時，回到現行的人工路徑並附原因。
- [ ] **T3 上限**：每個 run 連續自動同步設有上限（例如 3 次，可設定）。超過時停下並以明確 reason 說明（main 移動
      過快），不無限循環。
- [ ] **T4 證據與可觀測性**：自動同步的次數、每次的 main sha 與新 Candidate，寫進 run 的 evidence，
      `cortex work show` 可以看到。
- [ ] **T5 文件**：operator 文件說明 clean-behind 的自動處理與上限。新增 changelog fragment，並同步
      `CHANGELOG.md [Unreleased]`。
