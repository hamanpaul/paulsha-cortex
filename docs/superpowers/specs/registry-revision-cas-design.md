---
status: accepted
work_item: registry-revision-cas
---

# JobRegistry revision CAS Design（#966）

## Decisions

### D1 — Child boundary is the registry transaction contract

實作重心放在 paulsha_cortex/coordinator/registry.py：把載入的 state revision 綁到每一次整檔持久化，並以 per-state transaction lock 序列化 compare-and-persist。測試使用 tests/test_registry_single_writer_lock_818.py，並從實際 Manager control request 路徑驗證 conflict acknowledgement。

本 child 不建立 Manager lifetime owner lock、不改 run_loop 啟動所有權、不改 doctor、不掃 /proc。這些是依賴 CAS 的 #967。現況並未找到可重用的 exact-revision CAS／transaction-lock 原語或指定 race test module；目前 _reload_if_changed() 只比較 mtime／size，_persist() 直接呼叫 atomic writer，v1 migration 另有直接 state write。

### D2 — Revision 是 in-memory raw-content identity

在 JobRegistry 保存 loaded revision（建議 _state_revision），值為實際 raw bytes 的 SHA-256；缺少 state file 用 None 表示。讀取 state bytes 時只讀一次，將同一份 bytes 同時供 digest、JSON parse、migration 備份使用，避免 revision 與解析內容來自不同讀取。

提供單一 helper 讀取目前 durable revision。只把 FileNotFoundError 視為 absent；其他 OSError 不能降級成 absent。_reload_if_changed() 每次讀 bytes／digest；mtime 與 size 只保留 metadata，不允許同值時短路。成功 persist 後在 transaction lock 尚持有時從已提交 bytes 更新 _state_revision，或由 atomic writer回傳其精確 committed bytes digest。

raw digest 不持久化到 JSON，不新增 schema 欄位。bytes 格式、換行或排序等任何改變都構成 revision change；這是 exact-revision CAS 的定義。

### D3 — Canonical entry 固定 transaction lock 身分

新增 canonical_state_path(state_path) 與 state_transaction_lock_path(state_path)。canonical 規則為 resolve state path 的 parent directory 後接回原 filename，不 resolve jobs.json file symlink 本身。lock path 為 canonical state path 加上 .transaction.lock。

在 lock helper 中建立 lock parent directory，以 O_RDWR | O_CREAT、無 O_TRUNC 開啟 sidecar，blocking flock LOCK_EX。release 順序是 LOCK_UN、close；sidecar 永不 unlink、不寫 owner data。不同 symlink-directory／.. 路徑只要指向同一個 state directory entry，就得到相同 lock path。state file 自身為 symlink 時，lock identity 對應該 symlink 所在的目錄項，符合 atomic os.replace 實際替換目標。

這把短期 transaction lock 與 #967 的 <state>.writer.lock lifetime lock 分離，不能共用 fd 或 inode；不然同一 process 重取 flock 可能自我阻塞。

### D4 — _persist() 以 compare-then-commit 作為唯一一般寫入閘

一般 persist 的次序固定如下：

1. 建立 state parent directory，取得該 canonical state entry 的 transaction lock。
2. 在 lock 內讀取目前 durable raw bytes／revision。
3. 比較 actual revision 與本 instance 的 _state_revision。
4. 不相等：不呼叫 _write_payload_atomically；用 read-only／no-repair 載入路徑將 registry memory 恢復到目前 durable snapshot，建立 RegistryRevisionConflict(expected_revision, actual_revision, canonical_state_path)，最後釋放 lock 並 raise。
5. 相等：呼叫既有 _write_payload_atomically，保留其 temp file、fsync、rollback 行為；write 成功後更新本機 revision，再釋放 lock。

若 atomic writer 失敗，維持它現有 rollback／例外語意；memory recovery 讀取 rollback 後實際 state。任何恢復載入都不可在已持有 lock 時遞迴呼叫 _persist 或重新取得同一 lock。conflict 不是重試訊號；呼叫端要先明確處理，再以新 registry/snapshot 發起新操作。

expected/actual revision 與 state_path 作為 exception attributes 可由呼叫端測試與觀測；None 必須清楚呈現為 absent。

### D5 — Memory restore 必須取代 stale state，而非只補載欄位

目前 v2 _load() 以 max(seq, self._seq) 避免 sequence 往回；這不適用於 conflict recovery，因 stale writer 的本地 mutation 可能已增加 _seq。恢復模式需清空並完全重載 durable snapshot，sequence 使用 durable seq；state absent 時回到 constructor 傳入的 seq_start baseline。jobs、slices、workflows、legacy_records、reclaim_resets、revision 及 metadata 均應從同一 durable snapshot 建立。

恢復載入要避免再次持久化舊 schema repair：將現有 load/validation 拆出 read-only restore 路徑，或加入明確的 persist_repairs=False 模式。正常初始載入的既有 validation 與 repair 行為保留；衝突 recovery 只驗證／載入，不做 migration 或 normalization write。若實際 durable snapshot 無法解析，維持 fail-closed，不可改寫損壞 bytes。

### D6 — v1 migration 和 v2 normalization 共用同一 CAS boundary

v1 路徑現在直接備份後 atomic replace，會繞過一般 _persist()。migration 需以載入 v1 raw digest 作 expected revision，取得同一 transaction lock 並先比較 actual；只在相同時以目前 bytes 建立既有不可變 v1 backup，再以既有 atomic writer 寫入 v2 payload，最後記錄新 revision。可抽出接收 expected revision 與 optional before-replace callback 的 guarded commit helper，避免 migration 巢狀呼叫已持鎖的 _persist()。若 actual 不符，不能先建立 stale backup，也不能改 state。

v2 normalization 保留目前語意，但其 _persist() 必須走 D4。若 normalization 與另一 process 的 commit 競爭，舊正規化結果必須 conflict 並 reload 最新 durable snapshot。以 source search 與 tests 驗證不存在其他由 JobRegistry 寫 state path 的 bypass。

### D7 — Control request 保留既有 exception-to-error adapter

目前 manager_daemon.run_loop 對 executor 例外建立 done.status="error"，保留 exception type/message，成功 persist done 後才 unlink request。對 RegistryRevisionConflict 的整合測試需證明 done error 含 expected／actual／path，並證明 action 未以 done.status="ok" 宣稱成功。只有 error envelope durable 後 request 才能被消費；若 _persist_done 失敗，既有路徑需保留 request。

第一版不預設修改 manager_daemon.py；若整合測試證明一般 exception adapter 會漏掉診斷、回 ok 或先消費 request，才以最小變更修復，並重算 sizing。不要把 request error 當成 Registry CAS 自動 retry 或操作已完成。

### D8 — 驗證以真正的 process boundary 和現有 durability contract 為準

指定 race 測試 module 使用 subprocess barrier，讓各 process 在 mutation 前回報已完成 JobRegistry construction／載入同一 revision。不同 slice 測試可接受兩筆皆 durable，或明確 conflict；CAS 方案預期其中一方 commit、另一方 conflict。same-slice 測試固定競爭並驗證 deterministic conflict branch。所有結果要檢查 raw JSON、每個 process 的回傳／例外與 conflict 後的 memory，不只看 exit code。

另外驗證相同 size／mtime 的 raw replacement、transaction-lock holder 被 SIGKILL 後新 writer 可進入、sidecar 留存且無 owner record、v1 migration/v2 normalization、公開 method return compatibility，以及既有 atomic rollback test。這些 tests 驗證本 child 的確定性契約；不代替 #967 owner／doctor process inventory 測試或 #818 parent owner/risk cases。

### D9 — 五維 sizing（依 fix-standard 現況）

- domain_breadth = 0：production change 預計限於一個模組 registry.py。若 request integration 測試證明必須改 manager_daemon.py，重新宣告並重算。
- state_consistency = 2：跨 process exact revision、durable commit、conflict rollback/reload 與 crash release。
- acceptance_surfaces = 2：current_sizing_snapshot() 固定傳入 process-level 規則 R-09／R-16／R-19；fix-standard 有 2 個 gate_spine，訊號為 2 + 3 = 5，依門檻得 2。這是目前 sizing adapter 的真實輸入，即使本 child 不新增 CLI。
- spec_stability = 0：需三份 accepted artifact 完整且無 blocker，依 stability-risk-v2。
- orchestration = 2：fix-standard 的 9 張卡均有 persona_binding。

預估總分 6（Yellow），不是完整 #818 的 sizing。實際實作前須以當時 linked combo／contract rules 呼叫 compute_sizing_score()；combo、規則或 production module 範圍變動時，以新結果為準。不得以排除 #966 acceptance 來降低分數；若 sizing 變為 Red，回報完整 child scope 的 issue-backed split，不能靜默縮窄。

## 與 #967 的明確分界

- 本 child：JobRegistry durable revision、transaction lock、conflict exception、memory restore、migration／normalization write guard、control request 明確 error、CAS 多 process tests。
- #967：Manager lifetime owner lock、第二個 Manager fail-closed startup、owner 診斷、doctor 所有 live Manager collision groups 與 inventory-incomplete 回報。
- 本 child 的 transaction lock 不代表 Manager ownership，也不盤點 Manager processes。#967 依賴本 child 完成；#818 仍須等待兩者與父票 owner/risk 驗收。
