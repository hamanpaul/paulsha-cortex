# #838 跨 instance 共享的原子 quota reservation authority

新增 `paulsha_cortex/coordinator/quota_reservation.py`：跨 instance 共享的原子
reservation authority（`reserve`／`bind`／`settle`／`release`／`reconcile`），
以檔案鎖序列化同一份 append-only JSONL 事件檔完成「同池搶最後一單位恰好一個
成功」與「多 pool all-or-none」；沿用 #836 的 pool／window／unit 契約
（`authority_id`／`account_id`／`pool_id`／`revision` + `window_id`），刻意
不含 model 維度，換 model 名字無法拆池。每一次 reserve 綁定
`run_id`／`card_id`／`decision_id`／`attempt_id`／`observation_version`／
`demand_version`；owner token + attempt + CAS sequence 三者共同保護
bind／settle／release 不被錯誤呼叫端誤用；crash／restart 後只能靠明確的
`reconcile()` evidence 導向終局——lease 到期本身永不釋放容量，未確認前一律
回報 `uncertain`。同 decision 同組成的重複 reserve 冪等回放（`duplicate`），
組成不同則 `conflict`，不會靜默選邊；terminal 事件重送同樣冪等。新增
`quota_reservation_root()` path 與 Trust Root `quota-reservation-authority`
資產（Manager-owned、Monitor 唯讀），trust-root registry／permgen／install
plan 測試皆已納入涵蓋。

本票刻意不做候選排序、fallback、forecast（留給 #839）、不接線任何實際 spawn
path——模組本身保持 dormant／shadow，`reservation_authority_enabled()` 是
保留給未來整合者的 opt-in 開關（預設關閉即 rollback，不需要改程式碼）。

新增 22 個測試（`tests/test_quota_reservation_838.py`）覆蓋兩 process barrier
競爭（multiprocessing + Barrier）、多 pool all-or-none、failpoint 注入的
crash matrix（reserve／bind／settle／release 各階段）、negative（lease 過期
非活 job、liveness unknown、錯 owner/CAS/attempt、torn-write 損毀 store、
負值/非有限需求）、replay/reset 冪等性，以及 bounded worker 壓力測試與固定
輪數 release／reacquire 耐久 audit。
- #838 對抗審查修復：reservation 事件折疊驗證合法狀態轉移（終局後不得再轉移、bind 只能從 reserved），並驗證 reconcile 的 renew_lease_ms 與所有轉移的 event_at_ms／sequence 型別；損毀但 shape 合法的歷史一律 ReservationCorrupt。
- #838 對抗審查修復（第二輪）：release 只允許在 spawn 前（reserved），bound 之後只能經 settle 或 reconcile 結束（避免冪等回放交回 owner_token 後釋放活 job 的 lease）；讀回時比照 API 驗證 reconcile evidence 與 reserve row 的 window_id。
- #838 對抗審查修復（第三輪）：協定改為 reserve → bind(job_id) → spawn，settle 只接受 bound、release 只接受 reserved；reserve row 讀回驗證 pools 與 capacity_by_pool 逐一對應；首次建立 store 時 fsync 父目錄。
- #838 對抗審查修復（第四輪）：reserve row 讀回的 pool_ref／amount 驗證錯誤一律 ReservationCorrupt，並驗證 event_at_ms／created_at_ms／lease_expires_at_ms 範圍；補多時間窗獨立計帳測試，修正 reconcile 數值 fail-closed 測試使其不因 evidence 先失敗而空洞。
