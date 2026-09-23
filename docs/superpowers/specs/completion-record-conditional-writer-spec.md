---
status: accepted
work_item: completion-record-conditional-writer
issue: 996
---

# CompletionRecord 精確條件建立規格

這是 #977 的 issue #996 前置切片；GitHub issue #996，已建立。責任只限 `completion.py` 的單一 CompletionRecord durable writer contract。它不承接 Manager proof orchestration、WorkAuthority reload、remote closure、OutcomeStore append 或 registry CAS。#962 R1–R4/R6/R8(a–d) 與 #887 全部 AC 保持 aggregate gates。

## Problem

目前 `write_completion_record` 遇到已存在路徑時，會回傳任何可讀既存 row，沒有把它與 incoming proof-bound payload 比較；無效 existing row 則可能被移入 quarantine。`atomic_write_json` 的 no-clobber conflict 也回到此 helper。因而 caller 不能判斷「成功建立或精確重用」和「誤將別的 completion 當成本次成功」的差別。#977 不得把 #961 的 WorkAuthority arbitration 或 Manager daemon owner lock 誤當成 CompletionRecord writer serialization。

## Requirements

### I1 單一 production module 與新 API

只在 `paulsha_cortex/coordinator/completion.py` 新增語意清楚的 no-follow exact reader 與 conditional-create API，例如 `read_completion_record_exact(path)`、`write_completion_record_if_exact_or_create(payload, *, coordinator_root=None)`。不得修改 `write_completion_record` 既有行為或其他 production module，不新增 persisted fields/schema/CLI。若無法只在本 module 安全完成，停止並重算/拆票。

### I2 精確正規化比較

先用 `validate_completion_record` 驗證並正規化 incoming payload，再計算 deterministic path 與 canonical JSON hash。existing row 必須驗證出同一正規化 payload；比較完整 dict，包含所有 optional fields、`completed_at`、WorkAuthority 每個欄位和 volatile `snapshot_hash`、`provider_revision`、`source_revisions`。不得呼叫 `completion_records_semantically_match` 作 exact comparison，不得自行沿用 existing timestamp 或忽略 caller 欄位。

### I3 不存在時的 no-clobber durable publish

目標不存在時先建立同目錄暫存檔，完整寫入 canonical JSON，flush/fsync 檔案，再用不可取代目標 entry 的原子 primitive publish（例如 `os.link` no-replace）；完成後 fsync 父目錄。若目標在競爭中出現，轉入 existing-row exact comparison，不得用 replace/rename 覆蓋它。暫存檔清理只可移除本次建立的暫存 entry。

### I4 既存 row 安全讀取與 exact reuse

CompletionRecord target 及其 verification/review evidence references 都以 no-follow descriptor 開啟並確認 regular file；不得使用 `Path.is_symlink()` 後再 `Path.read_text()` 的分離檢查。以 anchored parent dirfd + `open(..., O_NOFOLLOW)` 或等效 primitive 開啟 entry，並比較 descriptor `fstat` identity 與 dirfd-relative no-follow stat（device/inode/file type）；讀取後再次比對 entry identity。若 entry 在 pre-open/open/read 期間被替換成 symlink 或其他 inode，拒絕本次讀取。以正規化 validator 和 descriptor-based reference validator 驗證內容/hash。incoming normalized payload 與 existing normalized payload 完整相等才回傳既存 `{path, hash, payload}`。#977 可先用此 reader 安全取得固定 completed_at，再組完整 incoming payload。

### I5 Conflict fail-closed 且 immutable

任一 mismatch、malformed/unreadable JSON、target/reference symlink、非 regular file、reference schema/hash failure、safe-read identity drift 或 write/fsync/publish error 都回傳可辨識的 conflict/error。API 不得 quarantine、move、unlink、replace 或 repair 已存在目標或 evidence entry，不得回傳不同 existing record 當成功。已存在內容保持 byte-for-byte 原狀。

### I6 冪等與競爭

兩個以上 process 對同一路徑並行：完全相同 normalized payload 至多建立一筆且所有成功 caller 得到相同 path/hash/payload；不同 payload 至多一個成功建立，其他 caller 明確 conflict。若 winning payload 的 `completed_at` 不同，不能視為同一 payload；Manager 之後須重新讀取並使用其固定 timestamp 重試。對 evidence reference 建立 deterministic race seam：在 safe reader 開啟 ref 前將 regular entry 替換為 symlink，以及 descriptor 開啟後、entry identity recheck 前再替換；兩者都必須 conflict、不得 publish CRecord，且不得改動/隔離被替換 entry。

### I7 舊 API 相容

`write_completion_record`、`read_completion_record`、既有 ship flow callers 的當前輸出、例外與 quarantine/read-back 行為須由回歸測試保護；新 API 與 legacy writer 明確分開，不以旗標或預設參數模糊副作用契約。

### I8 文件與 parent boundary

文件說明 conditional-create / exact-reuse / conflict contract、descriptor no-follow reference reads 與 caller 如何處理 fixed `completed_at`。#977 必須在每個持久化邊界前重載當前 WorkAuthority/source revisions 並重新跑 proof/closure；本 API 只 CAS 本地 CompletionRecord path，不驗遠端 authority。#976 只 CAS registry-local state。保留 #962 R1–R4/R6/R8(a–d)、#887 全部 AC；本 child 通過不代表其 aggregate 關閉。

## Non-goals

不改 OutcomeStore append/serialization、不提供跨 CompletionRecord、OutcomeStore、GitHub 與 registry 的分散式 transaction/CAS，不負責 current WorkAuthority refresh、proof API shape、read-only remote closure、Manager resume placement、registry terminal binding 或 deployment。
