---
status: accepted
work_item: outcome-store-conditional-cas
issue: 997
---

# Engineering OutcomeStore 多程序條件 append CAS 設計

## Decisions

### D1 — 每個 outbox 依實際 file entry 建 lock key

`OutcomeStore` 的 exact API 使用 canonicalized actual outbox path：絕對化並 resolve parent directories，但保留最後 basename/entry 身分。這與 registry 的 owner-lock key 分開；#967 只處理 `jobs.json`，不能鎖住/代表 `engineering-outcomes/<repo>.jsonl`。#977 需由注入的 registry/runtime state path 或同一配置直接構造 explicit `OutcomeStore.path`，不得用被注入 registry 脫鉤的 default `_run_state_path` 重算。

### D2 — sidecar flock 序列化所有 compliant outbox writers

canonical outbox path 對應固定 sibling lock file，例如 `<basename>.writer.lock`。`flock(LOCK_EX)` 包住 snapshot read、payload identity check、revision compare 與 publish；開鎖檔使用 no-follow、regular-file check、不 truncate、不 unlink。legacy `OutcomeStore.append` 亦使用此鎖和內部 revision-guarded writer，防止舊入口與新 API 互相丟列。

### D3 — revision CAS 保護整份 JSONL

Snapshot 將 raw bytes SHA-256 回報為 revision，missing 使用固定 sentinel。讀 final target 時以已 canonicalize 的 parent dirfd 開啟最後 basename，使用 no-follow descriptor、`fstat` regular-file 驗證、讀取後 descriptor/dirfd-relative no-follow entry identity recheck；不走 `Path.is_file()`→`Path.read_bytes()` 路徑。鎖內重新安全讀 rows/revision；每個非空 JSONL row 都用 `validate_outcome_record` schema validation/normalization，發現任何壞 row 或重複 `outcome_id` 即 fail closed，保留原 bytes。Conditional API 要 caller 提供取得 proof 後 fresh snapshot 的 expected revision。已存在唯一 outcome ID 時先 exact compare，exact row 是 zero-write idempotent return，不相等則 typed collision；ID 不存在但 revision 已變則 stale-CAS error。Temp file 完整寫入並 fsync 後，再用同一 safe no-follow reader 比對 target raw revision，通過才 atomic replace + directory fsync。

### D4 — logical append 不覆蓋其他 event

每次成功更新都是由最新、已驗證 revision 的完整 rows 增加恰一筆；不刪除或重新排序既有 outcome。相同 ID exact payload 只留下單列；不同 payload 即使 ID 相同也不得被靜默 dedup。對 different IDs 競爭，輸家遇 stale revision，fresh retry 後在最新 rows 加自己的 row，沒有 lost update。

### D5 — timestamp 是 exact payload

`emitted_at` 納入 strict equality。Manager 重入時先 `read_snapshot()`，若目標 ID 已存在，使用現存 exact row做 payload compare；要呼叫 conditional API 時必須提供其 fixed emitted_at。Store 不在 exact API 內替換 timestamp。若缺 ID但 outbox revision stale，必須回到 Manager 重跑 fresh proof/closure。

### D6 — errors preserve bytes

final outbox symlink/nonregular、descriptor/entry identity drift、任何 malformed/schema-invalid row、duplicate outcome_id、lock file symlink、不一致 revision 或任何 pre-publish error 都 fail closed；不使用 recovery/quarantine 來替換 outbox。safe-reader test seam 在 open 前 swap final entry 到 symlink，並在 descriptor open 後、identity recheck 前 swap 到 symlink/另一 inode；兩種都必須拒絕 snapshot/append 且 bytes 不變。Sidecar lock 持久存在，不能 unlink 後重建，以免不同 process lock 不同 inode。

### D7 — module and sizing boundary

唯一 production module 為 `coordinator/engineering_outcome.py`，domain_breadth=0。多 process 競爭同一 JSONL、sidecar lock、whole-file revision compare-and-replace、same-ID payload collision 與 retry/crash window 對應 state_consistency=2。正式 sizing 仍須以 repo `current_sizing_snapshot` 執行確認。

## API sketch

`read_snapshot()` returns `{path, revision, records}` for one canonical outbox directory entry. `append_if_exact_or_create(record, expected_revision=...)` returns `{path, revision, record, created}` after either creating one new logical row by revision CAS or verifying an exact existing row without write. A stale revision, same-ID different payload, malformed store, unsafe path, or lock/publish failure raises a typed error. The existing `append()` remains as compatibility entrypoint but must take the same lock and use the same revision-guarded publish; its established same-ID return behavior stays intact.

## Verification design

新增 focused OutcomeStore CAS tests與多程序 fixture。以 barrier 控制多個獨立 process：same ID/same entire payload (same emitted_at)；same ID/different payload；different IDs/stale snapshot；alias paths resolving to one parent entry；legacy `append` 與 exact API 競爭。fixture 必須含每欄合法 row、schema-invalid row、malformed JSON、duplicate same-ID identical row、duplicate same-ID different-payload row。斷言 rows 無遺失、IDs 不重複、exact duplicate bytes/revision不變、conflict不寫、stale caller必須重新 snapshot；所有 existing rows 在 append 前都先過 schema/unique-ID validation。注入 lock symlink、target final symlink/nonregular、pre-open symlink swap、post-open entry swap、descriptor identity drift、temp/fsync/replace fault與 crash before/after atomic replace；比較原始檔 bytes 和 parent-directory state。禁止測試藉由 `Path.is_file()` 或 `Path.read_bytes()` 實作 authoritative target read。

測試及 docs 明確說明這是 path-local compliant-writer CAS，不是 GitHub/authority/registry transaction。#977 要在 append 前由 Manager refresh WorkAuthority/source revisions、#975 proof、pure closure facts，並使用本次 snapshot revision；#976 仍只做 registry-local CAS。
