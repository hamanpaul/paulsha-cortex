---
status: accepted
work_item: completion-record-conditional-writer
issue: 996
---

# CompletionRecord 精確條件建立設計

## Decisions

### D1 — 加新 API，保留既有 writer

在 `completion.py` 提供獨立 conditional-create API。既有 `write_completion_record` 維持當前「已存在時沿用/衝突處理」語意，避免影響既有 ship/repair callers。新 API 明確承諾 exact reuse 或明確 conflict，供 #977 使用。

### D2 — create-only CAS

以 deterministic CompletionRecord path 為競爭 key。先將 normalized payload 寫入同目錄暫存檔並 fsync，再用 no-replace primitive 原子建立目標；不採用 `os.replace`。此操作只做 path-level create-if-absent CAS，不聲稱驗證 WorkAuthority 在外部來源仍 current；#977 在每個 durable boundary 前負責重新載入 authority/source revisions 並重跑 proof。

### D3 — exact existing-row identity and descriptor-safe references

暴露可由 caller 使用的 no-follow exact reader；目標先於本次寫入存在或 publish 時遭遇 `EEXIST`，都走同一安全 read/compare。CompletionRecord 與每個 verification/review evidence ref 均以 anchored parent dirfd 和 `O_NOFOLLOW`（或等效 descriptor API）開啟，使用 `fstat` regular-file check；開啟前後以 dirfd-relative no-follow stat 比較 device/inode/file type，偵測最後路徑 entry 的 symlink/other-inode swap。不得重用 `_validate_reference` 中分離的 `Path.is_symlink()` → `read_text()` 序列。以 `validate_completion_record` normalization 和 descriptor-based reference validation 驗證 existing record。整個 normalized payload 完全相等時才回傳 receipt；任何 hash 或 payload 差異都 conflict。

### D4 — 不移動衝突資料

新的 API 不呼叫 `_existing_record_or_raise`，因為那會接受不同 readable row 或 quarantine unreadable row。malformed、symlink、unreadable、unsupported file type、reference failure 或 identity drift 只回錯誤，不改 existing bytes。舊 API 不變。

### D5 — timestamp 屬於 exact identity

`completed_at` 和 optional fields 都納入比較。新寫入由 caller 指定 timestamp；相同 payload duplicate 得到相同 hash。若現存 row exact match，維持其 timestamp/bytes；若僅 timestamp 不同則 conflict。#977 應先安全讀取現存 row，以現存 fixed timestamp 組 incoming draft，再呼叫本 API做 exact verify/reuse；遇到 race 才可下次 fresh resume 重讀，不可在此 API 偷換 incoming 欄位。

### D6 — 單一 production module scope

唯一 production module 為 `coordinator/completion.py`；domain_breadth=0。record path 與其 verification/review evidence refs 是多個 filesystem entries；需用 descriptor identity 偵測競爭替換，再以 record path conditional create，故 state_consistency=2。fixed `fix-standard` combo 的 2 gate_spine 加 R-09/R-16/R-19=2 acceptance surfaces，accepted triad 的 spec_stability=0，9 cards/9 persona bindings 對應 orchestration=2；正式 sizing 須由 repo `current_sizing_snapshot` 實跑確認。

## API behavior

`write_completion_record_if_exact_or_create(payload, *, coordinator_root=None)` returns `{path, hash, payload}` only when it has atomically created a normalized record or safely read/validated an exactly equal existing record. A typed conflict/error identifies the stop class but must not include secrets. Existing target entries are immutable to this API. On creation, serialize exactly the normalized canonical record representation; on exact reuse, hash the existing normalized representation.

## Verification design

新增 focused tests，覆蓋 no-follow exact reader、absent-create、同內容 serial/idempotent reuse、不同欄位 conflict、process-level same/different payload races、completed_at conflict、optional/WorkAuthority volatile source field drift、malformed/unreadable/symlink/directory/nonregular target、reference failure、evidence-ref symlink entry swap（open 前與 descriptor 開啟後）、temp-write/fsync/link fault、crash before/after publish，以及 no-quarantine/no-overwrite byte snapshots。以同步 barrier 的 subprocess 測試驗證 OS-level no-clobber，而非僅用同 process mock。

另保留 `write_completion_record` 舊行為的 regression。文件列出新 API 不處理 remote source freshness：#977 必須在呼叫前重新讀 current WorkAuthority/source revisions，重跑 #975 proof 與純 closure inspection；只有 Manager 負責 outcome payload 外部比對，#976 只負責 registry-local CAS。
