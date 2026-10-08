## Tasks

- [x] **T1 RED**：新增測試，固定以下行為（現行應失敗）：
      - relation 為 `clean-behind` 時，Manager 自動把當下的 exact main 合進候選，產生新的 Candidate，並寫入
        main-sync evidence。
      - 只重跑 verify 與 ship probe，不重派 build 與 code-review，run 不設 needs_human。
      - relation 為 `candidate-conflicts-with-main` 或 probe 失敗時，行為與現行完全相同。
- [x] **T2 自動同步**：在 `_main_sync_stop_result` 的呼叫端實作 clean-behind 自動同步。合併由 Manager 以受控方式
      完成，不交給 model；合併失敗（例如同步當下 main 又前進並產生衝突）時，回到現行的人工路徑並附原因。
- [x] **T3 上限**：每個 run 連續自動同步設有上限（例如 3 次，可設定）。超過時停下並以明確 reason 說明（main 移動
      過快），不無限循環。
- [x] **T4 證據與可觀測性**：自動同步的次數、每次的 main sha 與新 Candidate，寫進 run 的 evidence，
      `cortex work show` 可以看到。
- [x] **T5 文件**：operator 文件說明 clean-behind 的自動處理與上限。新增 changelog fragment，並同步
      `CHANGELOG.md [Unreleased]`。
