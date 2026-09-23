---
status: accepted
work_item: registry-revision-cas
---

# JobRegistry revision CAS Specification（#966）

## Requirements

### 背景與範圍

父票 [#818](https://github.com/hamanpaul/paulsha-cortex/issues/818) 已證實多個 Manager／JobRegistry 可各自載入同一份 jobs.json，再以完整 snapshot 的 atomic replace 互相覆寫。Child [#966](https://github.com/hamanpaul/paulsha-cortex/issues/966) 交付 registry 層 exact-revision compare-and-swap（CAS）與明確 request conflict；父票另一個 child [#967](https://github.com/hamanpaul/paulsha-cortex/issues/967) 依賴本 CAS，處理 Manager lifetime ownership 與 doctor live-process inventory。

本規格只涵蓋 JobRegistry durable state commits、CAS conflict recovery、現有 Manager control request 的錯誤 acknowledgement，以及這些行為的測試。不得以本 child 完成宣稱 #818 已解決。

### R1 — 每個 state write 必須驗證載入 revision

每個 JobRegistry durable state write 都必須以該 instance 最近一次成功 load／commit 記錄的 revision 作為 expected revision。_persist() 持有以 state path 為鍵的跨 process transaction lock 時，先重讀目前 durable state；只有 actual revision 等於 expected revision 才可寫。

若 revision 不相等，writer MUST NOT 呼叫 atomic replace，不可保留或回報該次未落盤 mutation 為成功；必須恢復 memory 至 durable snapshot，並 raise 可辨識的 RegistryRevisionConflict。不得合併欄位、自動 retry 或重播原 transition。

### R2 — Revision 代表實際 bytes

Loaded revision 是 jobs.json 實際 raw bytes 的 SHA-256。狀態檔不存在使用與任何 bytes digest 不同的 absent sentinel（以 None 表示）；空檔案仍是其實際 bytes 的 digest。權限錯誤、I/O error、無法讀取或無法解析不得被當成 absent。

revision 不加入 JSON payload，不改 schema。CAS 寫入成功後，本機 revision 必須更新為實際落盤 bytes 的 digest；atomic write 失敗且 rollback 成功時，revision 與 memory 必須重新對齊 rollback 後的 durable state。

### R3 — Content change detection 不依賴 metadata

_reload_if_changed() 每次都需取得目前檔案內容 revision（或確認真正不存在），不可用 mtime／size 相同略過 bytes 比對。mtime／size 可保留作診斷 metadata，不能成為跳過 exact-content detection 的 gate。

因此，不同 bytes 即使檔案大小相同且 mtime 被還原成原值，也必須被視為新的 revision；持有舊 revision 的下一次 persist 必須 conflict。

### R4 — Canonical transaction lock

以 canonical_state_path(state_path) 產生穩定 state identity：先對 Path(state_path).expanduser() 的 parent directory 做非 strict resolve，再接回原檔名；不得 resolve state file 本身的 symlink。transaction sidecar 為 canonical path 後綴 .transaction.lock，例：jobs.json.transaction.lock。

每次 compare-and-persist 必須對此 sidecar 取得 blocking exclusive flock，並在同一 critical section 內讀取 durable revision、判斷、執行既有 atomic writer、更新本機 revision。lock fd 以 O_RDWR | O_CREAT 開啟，不帶 O_TRUNC；釋放只 unlock、close，sidecar 永不 unlink，也不寫 owner record。相同 canonical state entry 的不同目錄拼法必須共用同一把 lock。

### R5 — Conflict exception 與 memory recovery

RegistryRevisionConflict 必須提供可程式化欄位 expected_revision、actual_revision、state_path，且錯誤字串包含這三項診斷；state_path 是 canonical state path。absent sentinel 的錯誤摘要需可讀，不可與 SHA digest 混淆。

偵測 conflict 後不得寫入 stale payload。從當下 durable snapshot 恢復所有 registry memory：jobs、slices、workflows、legacy records、reclaim resets、sequence 與相關載入 revision／metadata 均須對齊；若 state 真正不存在，回到 constructor 的空 state／seq_start baseline。reload 用於 recovery 時不得觸發會再取得同一 transaction lock 的 migration／normalization write。若 durable snapshot 本身格式錯誤，保留既有 fail-closed 載入行為，不得覆寫它。

### R6 — Migration 與 normalization 也受 CAS 保護

所有由 JobRegistry 寫入 state path 的路徑都必須經過相同 compare-and-persist boundary，包括：

- v1 migration：目前 _load() 會直接執行 _write_v1_backup(original) 與 _write_payload_atomically(migrated)；migration 必須先驗證原始 v1 bytes 仍為載入 revision，再在同一 transaction lock 保護下建立原始內容備份並完成 atomic replace，維持備份與 migration 的既有順序／語意。stale migration 不得建立基於過期 bytes 的替換結果。
- v2 slice normalization：目前 _load() 會在 slice rows 被正規化時呼叫 _persist()；此修復 write 必須 CAS，且遇到 conflict 時不可把舊 normalization 覆蓋到新 snapshot。

不得存在繞過 CAS 的 JobRegistry state write。transaction lock 持有期間所需的 rollback/conflict reload 必須使用不會巢狀重取 transaction lock、也不會在 reload 內再次持久化修復的路徑。

### R7 — Control request 明確回報 conflict

若 Manager control request 的 executor 因 RegistryRevisionConflict 失敗，request 完成結果必須以 durable done.status="error" 回報，診斷包含 exception type、expected/actual revision 與 canonical state path；不得回 ok，不得宣稱該 action 已 durable 或已完成。

error done 是對「action 被拒絕」的明確 acknowledgement，不是成功 acknowledgement。request 檔只有在該 error done 已成功 durable 後才可被消費；不能先刪 request 或以成功狀態掩蓋 conflict。現有 Manager request loop 已有一般例外轉 error 的路徑，先用整合測試驗證；只有測試證明該路徑不符合本條時，才將必要的最小修正納入本 child 並重新評估 sizing。

### R8 — 相容性與 crash recovery

無競爭且 CAS 成功時，保持 jobs.json payload、schema_version、公開 registry method 回傳值及既有 atomic replace／fsync／backup／rollback durability 語意。不得以增加 JSON revision 欄位來改 schema。

若 process 在持有 transaction flock 時遭 SIGKILL，kernel 釋放 flock；新 JobRegistry 可取得同一 sidecar 並成功 persist。sidecar 檔案留存，沒有 owner record。此鎖只保護單次 registry commit，與 #967 的 Manager lifetime .writer.lock 不同。

### R9 — 可重現驗收

測試新增於 tests/test_registry_single_writer_lock_818.py，跨 process 情境使用真實 subprocess 與 barrier 固定兩個 registry 都已載入 revision N；不得用單 process mock 取代競爭條件。逐條驗收：

1. 不同 slice 的同 revision 雙 writer：持久結果必須保留兩筆 mutation，或一筆落盤、另一方明確收到 RegistryRevisionConflict；不可 silent overwrite。
2. 同 slice 的同 revision 雙 writer：至少一個 deterministic case 命中 conflict branch，且衝突方不可回報成功。
3. conflict 後檢查 stale registry memory 已回到 durable snapshot，並檢查 exception 的 expected／actual revision 與 canonical state path。
4. Manager control request 觸發 conflict 時，檢查 durable done.status="error" 與 conflict 診斷；不得 ok 或將 action 當作已完成。
5. 以不同 bytes、相同大小、恢復原 mtime 的 replacement，驗證精確偵測及 stale persist conflict。
6. subprocess 持有 transaction lock 遭 SIGKILL 後，以 fresh registry 成功 persist；sidecar 留存且無 owner record。
7. 驗證 migration、normalization、一般 persist 全經 CAS；保留 payload/schema/public returns 與既有 atomic rollback regression。
8. 至少一組真正的雙 process barrier 測試留在上述指定 test module。

## 父票與 child 邊界

- #966 是 registry exact revision CAS、per-persist transaction lock、conflict memory recovery 與 control request error acknowledgement。
- #967 是 Manager lifetime ownership／startup rejection、owner diagnostic 與 doctor 完整 live Manager /proc inventory；其 dependency 是 #966。
- #966 不包含 lifetime owner lock，也不包含 doctor inventory。兩個 child 都完成並通過 #818 原有 owner/risk 測試後，才由父票決定是否關閉 #818。
