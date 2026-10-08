## Tasks

- [x] **T1 RED**：重現「delivery journal 已有 active row → authority-restart 讓 `WorkflowRun.claim_key` 換代 → resume」時回 `persisted workflow does not match current WorkAuthority`。用正式的 revision 格式與 journal 生命週期，不要用假的 fixture 字串。
- [x] **T2 最小修正**：era 換代時，讓 delivery journal 的 active row 跟上 `claim_key`。可以在 authority-restart 時同步更新，或讓 `_load_work_run` 在 claim era 經過驗證後（`authority_matches_claim_era`）同步 `claim_key`。不新增優先於既有 resume 分支的檢查；不放寬其他欄位的比對。已經卡住的現有 run，升級後 resume 也要能自動恢復。
- [x] **T3 不退化測試**：審查列出的 3 個案例（PR 已合併時的 merged-delivery-closure；head 等於 candidate 且 readback 失敗時的 active-workflow；OpenSpec revision 變動時 reason 要正確），都要有測試，結果與 base 相同。
- [x] **T4 恢復路徑**：PR 已存在時，`regenerate-gates`→`resume` 能採信已通過的 build；同一個 candidate 上 `retry-review` 之後，resume 能派出替代的 review job。
- [x] **T5 fail closed**：PR 改綁、issue 關閉、openspec mapping 變動時拒絕，reason 正確。
- [x] **T6 文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
