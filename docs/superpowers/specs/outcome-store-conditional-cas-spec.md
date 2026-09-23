---
status: accepted
work_item: outcome-store-conditional-cas
issue: 997
---

# Engineering OutcomeStore 多程序條件 append CAS 規格

這是 #977 的 issue #997 硬前置；GitHub issue #997，已建立。production scope 僅為 `engineering_outcome.py` 的 canonical outbox path、revision snapshot 和 conditional append。#977 負責 fresh WorkAuthority/source reload、#975 proof、remote closure 與 expected outcome 建構；本票只 CAS 本機 OutcomeStore。#976 只 CAS registry-local state。#962 R1–R4/R6/R8(a–d) 與 #887 全部 AC 保留。

## Problem

`OutcomeStore.append()` 目前 read JSONL → outcome_id 查重 → tempfile rewrite + `os.replace`，無 process-shared lock，也沒有 expected file revision。相同 ID 的不同 payload 會回傳 existing row；不同 ID 的 concurrent read/replace 會遺失先寫入者。#967 lock key 是 registry `jobs.json` entry，無法保證它和 injected registry 推導出的 outbox path 一致，亦不涵蓋所有 outbox writer。故 #977 不可把 owner lock 或 append 的 outcome_id dedup 當成 outbox CAS。

## Requirements

### I1 單一 production module 與 explicit API

只修改 `paulsha_cortex/coordinator/engineering_outcome.py`。提供可由 caller 使用的 canonical read snapshot 與 strict conditional append API，例如 `OutcomeStore.read_snapshot()`、`append_if_exact_or_create(record, *, expected_revision)`。不得新增 schema/CLI、修改 Manager/registry/completion 或引入其他 production module。

### I2 Outbox path canonicalization

writer CAS 的 path key 以實際 OutcomeStore path 為準：先轉絕對路徑、canonicalize parent directory，再保留最後 basename 作為將被 atomic replace 的 directory entry；不得 `resolve()` 跟隨 final JSONL file/symlink。相同 filesystem entry 的 relative/absolute/parent-symlink aliases 必須取得同一 canonical path/sidecar lock；final path 是 symlink 或非 regular file 一律 fail closed。不得從 `registry` default `_run_state_path` 猜 store path；#977 必須把與 injected registry/runtime configuration 相符的明確 outbox path 傳進 store。

### I3 Snapshot revision、safe target reader 與 path-local interprocess lock

snapshot revision 是 raw JSONL bytes 的 SHA-256（file missing 使用固定 absent sentinel）。最終 outbox target 必須以 canonical parent directory descriptor 錨定，透過 dirfd-relative `open` 搭配 `O_NOFOLLOW|O_RDONLY|O_CLOEXEC` 開啟最後 basename；不得用 `Path.is_file()` / `Path.read_bytes()` 或 `is_symlink()` 後再路徑讀取。`fstat` 必須確認 descriptor 指向 regular file；讀取前後再用 descriptor `fstat` 與 dirfd-relative no-follow `stat` 比較 device/inode/file type。entry missing、symlink、非 regular file 或 identity 在讀取期間改變時，回 typed unsafe-target/conflict，不回 snapshot、不寫入。新增 deterministic seams：open 前將 final entry 換成 symlink 必須由 `O_NOFOLLOW` 拒絕；descriptor 開啟後、entry recheck 前換成 symlink/其他 inode，必須由 identity compare 偵測並拒絕。parent directory 由 canonicalizer resolve 後以 directory descriptor 固定；final basename 永不跟隨。

由 canonical target 推導固定 sidecar lock path。使用 process-shared exclusive OS lock；sidecar 以 `O_NOFOLLOW|O_RDWR|O_CREAT`（或同等安全 flags）打開、檢查 regular file、不 truncate、不 unlink。所有本模組 durable writers（含 legacy `append`）必須共用同一 canonical lock key，不能只鎖 Python thread 或 Manager PID。

### I4 Strict exact conditional append

`append_if_exact_or_create` 先 validate/normalize incoming record，再在 lock 內安全讀取最新完整 JSONL 與 revision。若 `outcome_id` 已存在，必須比較完整 normalized payload 每一欄（包含 `emitted_at`）；完全相等時回既存 row/當前 revision、零寫入，任何差異回 typed outcome-ID conflict且不改檔。不可把同 ID 當成等價，也不可忽略時間或 optional欄位。

### I5 File revision CAS 與 non-clobber logical append

若 ID 不存在，caller 的 `expected_revision` 必須精確等於 lock 內讀到的 revision；stale revision 回 typed conflict、不寫入，讓 Manager fresh reload authority/proof/closure/outbox snapshot 後再判斷。寫入保留全部既有 rows/順序並只新增一筆。temp write/fsync 後、publish 前再次確認 target raw revision 與 expected revision 相符；只有 compare 成功才原子替換整份 JSONL 並 fsync directory。此流程不得覆蓋不同 revision 的 concurrent append；lock、revision check 與 canonical target path 必須在所有 compliant writers 間一致。

### I6 Retry and legacy writer compatibility

同 ID 同 complete payload 的多程序競爭最多寫一筆，其他 caller 可取得 exact existing row；若兩 caller 的 expected revision 過時但 exact row 已由 winner 建立，strict API 可在 lock 內先完成 exact payload compare 並以 zero-write no-op回傳；不同 payload 必拒絕。新 ID 的 stale CAS 要求重新讀 snapshot。既有 `append` 保留 current same-ID return semantics，但改為使用相同 lock/revision guarded publish，避免與新 API 或另一個舊 caller 相互遺失 append。

### I7 Fail-closed read/write errors

每一列非空 JSONL 都必須 JSON decode，並以既有 `validate_outcome_record` 做 schema validation/normalization；`outcome_id` 在同一 outbox 必須唯一。任何 malformed row、schema-invalid row 或 duplicate outcome_id（不論內容相同或不同）都回 machine-diagnosable error，整個 operation fail closed；不得略過壞列、挑其中一筆、去重修復或 append。target safe-reader identity drift、target symlink/nonregular、lock symlink/nonregular、permission/read errors、lock acquisition error、revision mismatch、temp/fsync/replace error 也回可診斷錯誤，不清空、truncate、quarantine、unlink 或覆寫 outbox。publish 前 failure 保持原 bytes；publish 後 retry 必須能由 fresh snapshot 冪等收斂。

### I8 Tests/docs/parent boundary

用 subprocess barriers 驗 multi-process same/different ID races、payload collision、stale revision、aliases、legacy writer interaction與 crash/fault；另外逐列驗證 schema，對重複 outcome_id fail closed，並以 deterministic seams 測 final target open 前與 descriptor 開啟後的 symlink/entry swap。文件清楚說明 CAS 範圍只涵蓋 canonical outbox file；#977 Manager 每個 durable boundary 前仍須 reload current WorkAuthority/source revisions、重跑 #975 proof/closure；#976 只 CAS Registry local state。保留 #962 R1–R4/R6/R8(a–d) 和 #887 全部 AC。

## Non-goals

不取得 #967 Registry owner lock、不要求 outbox lock path 與 `jobs.json` lock 相同、不做 remote/authority/registry 跨系統 transaction，不改 `OutcomeStore` record schema/outcome ID formula，不改 Manager orchestration，也不關閉 #977/#962/#887。
