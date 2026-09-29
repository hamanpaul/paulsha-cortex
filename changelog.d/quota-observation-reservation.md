# quota-observation-reservation

- **`#836` shadow 模式記錄 Cortex 自家受管 job 的終局用量**：periodic tick 的
  `manager.reconcile_quota_admission_reservations()` 新增第三步
  `harvest_quota_terminal_usage()`，shadow 與 enforce 都執行——依 registry
  與 admit receipt 的事實（enforce 用 `quota_decision_id` 精確比對；shadow 用
  attempt ordinal＋`selected` executor／model_id，對不上或有歧義就不記）把
  已終局 workflow job 的 usage 經既有 `_quota_admission_record_terminal_usage`
  寫進 quota ledger。idempotency key 與 enforce settle 路徑相同，重複
  harvest、重啟、`on_settled` 先記過都只有一份；記到一半 crash 的缺口下一輪
  補齊。結果以 `terminal_usage`（`recorded`／`failed`／`skipped` 理由）進
  daemon tick summary。`QuotaShadowService.record_terminal_usage()` 新增選填
  `known_idempotency_keys`，命中已在 ledger 的 key 直接算 duplicate，不再逐筆
  重讀 ledger；`quota_ledger.caller_idempotency_key()` 統一 caller key 前綴。
- **`#836` 測試**：新增 `tests/test_quota_terminal_usage_harvest_836.py`——
  controller／worker／reviewer 三個 profile 同帳號經 production helper 記帳後
  short／week 剩餘量恰為 snapshot 減總用量（重疊 group binding、原樣重播、
  重啟、跨 profile 重播都不多扣）；真 `dispatch_workflow_card`（shadow）＋
  真 `update_headless_result` usage 抽取後 periodic reconcile 收割一次、再跑與
  重啟不增 row；enforce 的 `on_settled` 與收割不重記且 reservation 釋放後只剩
  usage 一份扣減；ordinal／identity／歧義／精確 decision id／半途 crash／缺
  usage／缺 unit mapping 的收割判定。
- **`#838` crash matrix 補 after-append failpoint**：`QuotaReservationAuthority`
  的 reserve／bind／settle／release／renew／reconcile 在事件 fsync 落地後多一個
  `<stage>-after-append` failpoint（production 預設 no-op），模擬「已落地、呼叫端
  還沒拿到結果就 crash」。測試證明重啟後依舊 sequence 重送：reserve 拿回同一筆
  grant 與 owner_token、bind／settle／release 回 duplicate、renew／reconcile 被
  CAS 擋下，一律不再 append、不改 committed；bind 冪等只對同一個 job_id 成立。
- **`#838` 重啟不重派與 AC5 交叉扣減測試**：Manager 重啟（新 registry／authority／
  store／ledger instance）後 periodic reconcile 對仍在跑的 bound job 只做
  confirmed-alive 續租，連兩輪 `resume_workflow_run` 回 `in-flight`、不 launch、
  不 reserve、不寫新 receipt；window reset 後有效 reservation 仍計入新 capacity；
  job 跑的期間只有 reservation、終局後只有 ledger usage，兩者不同時計、重送不
  多扣；跑到一半的 snapshot 已反映消耗時終局 usage 判 straddling-usage（unknown，
  不做第二次扣減、enforce 下不可行）；舊 window 的 job 在 reset 後才 settle 不扣
  新 window。
- **`#838` coverage gap 機讀輸出**：新增 `quota_reservation.authority_coverage()`
  （schema `cortex/quota-reservation-coverage/v1`）明列跨 host
  （`reservation-authority-host-local-flock`）、跨 UID
  （`reservation-store-owner-only-permissions`）、跨 coordinator root
  （`reservation-authority-scoped-to-coordinator-root`）不協調；新增唯讀
  `cortex quota reservations [--store <path>] [--json]` 輸出實際解析到的 store
  路徑、狀態計數、uncertain、committed 與上述 gap。README 補 authority 路徑的
  解析順序與「同帳號多 instance 要共用必須同 coordinator root、user 級與
  system 級（不同 UID）目前無法共用」的部署說明。
- **`#838` 移除死開關 `PSC_QUOTA_RESERVATION_ENFORCE`**：
  `quota_reservation.reservation_authority_enabled()` 從未有 production 讀取點，
  `quota_admission` docstring 卻寫「兩者都要 on」、README 寫「各自獨立」。移除後
  唯一的 enforce 開關是 `PSC_QUOTA_ADMISSION_ENFORCE`（enforce ⇒ 原子預留），
  docstring／README 同步改正；設過舊變數的部署行為不變（它本來就沒有作用）。
