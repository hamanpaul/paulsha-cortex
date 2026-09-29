# #843 recovery action 契約矩陣驗收缺口

- 反射確認每個 work recovery action 在 `execute_work_action` 有實際分支；新增機讀 gap ledger（`docs/recovery-action-gaps-843.json`），closed 項須指向實際存在的驗收測試、open 項的可重現證據須指得到測試，full-plan task 5.5 未解除前 ledger 必須仍有未關閉項。
- 新增 `tests/test_recovery_action_state_matrix_843.py`：14 個 work recovery action 各以既有 producer fixture 建情境，經正式入口送出後以全新 `JobRegistry` 重讀與 daemon status provider 的 attention 投影核對（R02 正例）；錯 phase／錯卡／active job／缺 actor 或 reason／stale exact-run／錯狀態的負例拒絕後 registry、檔案、job 數與 attention 全不變，每個 action×類別不是 probe、冪等 no-op、strict xfail 缺口就是寫明理由的 N/A。
- 同檔 R03：在 admission 讀完、第一次持久化之前由另一個 registry writer 提交新 generation／Candidate／slice binding／superseded 世代，正式入口一律 fail closed、持久狀態等於 drift 後狀態；R09：attention 投影可能提供的每個 recovery action 都有情境，並只用 attention 條目組請求送回 `execute_work_action`。
- R05：recover-planning 經 daemon `work-action` 請求零 job，production periodic tick 下一拍派出 `writing-plans` planner job，brainstorm 必要時重進 define producer（`test_recover_planning_brainstorm_authority_728.py::test_r05_*`）。
- R06：work 與 slice `retry-verify` 以同一 namespace-adapter fixture 從 daemon request executor 跑同一組斷言（`tests/test_retry_verify_shared_conformance_843.py`）。
- R07：recover-superseded 在舊 attempt 仍 active 時拒絕，延遲 terminal 送達並恢復後不被新 era 採信（`test_recover_superseded_776.py::test_r07_*`）。
- 新發現的 producer 缺口以 strict xfail 與 ledger 列管、不寫成通過：pre-candidate admission 不看 active writer、abandon／retire-delivered 的 engineering outcome 先於 registry CAS、帶 PR refs 的未交付 run 仍投影必拒的 abandon、slice 同一 Candidate 重跑 evidence 路徑衝突、reclaim dirty scan 失敗仍續行刪除；R08 multi-UID reclaim 評估為生產端阻擋並列明重驗條件。
