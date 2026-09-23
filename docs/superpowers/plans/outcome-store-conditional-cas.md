---
status: accepted
work_item: outcome-store-conditional-cas
domain_breadth: 0
state_consistency: 2
invariant_count: 8
artifact_classes:
  - source
  - tests
  - documentation
issue: 997
---

# Engineering OutcomeStore 多程序條件 append CAS 工作計畫

唯一 owner：issue #997 `outcome-store-conditional-cas`（GitHub issue #997 已建立）。此票是 #977 的 hard prerequisite，只實作本機 outbox path-local CAS；不實作 Manager proof/finalizer。#962 R1–R4/R6/R8(a–d) 與 #887 全部 AC 保持 aggregate gates。

Planning views：[spec](../specs/outcome-store-conditional-cas-spec.md)、[design](../specs/outcome-store-conditional-cas-design.md)。

## Boundary

唯一 production diff 為 `paulsha_cortex/coordinator/engineering_outcome.py`。新增 canonical path/revision snapshot、outbox sidecar process lock、strict same-ID payload compare 和 expected-revision conditional append。既有 `append` 維持 same-ID compatibility semantics，但所有 durable writers 必須共享 lock/revision guarded publish。不得改 registry/manager/completion、outcome schema、outcome ID formula 或 CLI。

測試位於 `tests/test_engineering_outcome.py` 或新增 focused module。PR 依 repo policy補 changelog fragment、CHANGELOG [Unreleased]、docs/unified-work-lifecycle.md、CLI help compatibility、required tests 與帶 PR context 的 policy check。

## Tasks

- [ ] **T1 tests / RED snapshots and exact identity (I1, I2, I4–I6)**：定義 canonical path/revision snapshot；same outcome ID 完整 exact row可 no-op，不同任何欄（含 emitted_at）衝突。
- [ ] **T2 source / canonical entry key and secure lock (I2, I3, I7)**：加 path canonicalizer：絕對化、resolve parent、保留 final basename；canonical sidecar lock使用 `O_NOFOLLOW`/regular-file檢查、獨立 process exclusive lock，不 unlink。
- [ ] **T3 source / safe outbox snapshot and CAS (I1, I3–I5, I7)**：以 canonical parent dirfd + final-basename `O_NOFOLLOW` descriptor 讀完整 JSONL、確認 regular file，讀前後用 `fstat` 與 dirfd-relative no-follow stat 比對 identity；不呼叫 `Path.is_file()` / `Path.read_bytes()`。逐列 `json.loads` 後交既有 `validate_outcome_record` 正規化，並對重複 `outcome_id` fail closed；再計 raw-byte revision。expected revision mismatch typed stop；同 ID 唯一 row 先 exact compare；absent ID 保留全部既有 rows。
- [ ] **T4 source / atomic conditional append plus legacy serialization (I3–I7)**：temp write+fsync、publish前 revision recheck、atomic replace+directory fsync；所有 mutating entrypoints (包含 append) 持相同 canonical lock並走 revision guarded publish；exact API對 collision fail closed。
- [ ] **T5 tests / multi-process races and path identity (I2–I6)**：subprocess barrier驗 same-ID same/different payload、different IDs、stale snapshots、relative/absolute/parent-symlink aliases及 legacy/new writer concurrent；無 lost update、無 duplicate、stale append不寫。
- [ ] **T6 tests / schema, identity swaps and crash faults (I6, I7)**：測每列 schema-invalid、malformed JSONL、duplicate same-ID identical/different rows、final symlink/nonregular、lock symlink；deterministic seam 在 final open 前及 descriptor 開啟後、identity recheck 前 swap final entry 為 symlink/另一 inode，都必須拒絕且不改 bytes。測 permission、temp/fsync/replace errors 與 crash 前後；conflict前 bytes不變，publish後 exact retry只留一列。確認 authoritative read 不用 `Path.is_file()` / `Path.read_bytes()`。
- [ ] **T7 documentation / parent contract (I8)**：說明 path-local CAS、Manager傳 explicit outbox path 和 fresh snapshot revision、Manager仍負責 external authority/proof、#976仍只負責 Registry-local CAS；保留 #962/#887 AC。
- [ ] **T8 documentation / required gates and closeout (I8)**：更新 API/lifecycle docs/changelog/CHANGELOG；CLI help compatibility、focused tests、repo required tests、含 PR context policy check；分開回報本 child、#977、#962、#887 狀態。

## 五維 sizing

使用 repo `work_bridge.current_sizing_snapshot` 對 accepted plan/spec/design、`fix-standard`、完整 `ACCEPTANCE_SURFACE_RULES` 實算：

| 維度 | 分數 | 推導 |
|---|---:|---|
| domain_breadth | 0 | 唯一 production module 為 `coordinator/engineering_outcome.py`。 |
| state_consistency | 2 | 多 process 對同一 JSONL 作 exact same-ID compare、whole-file revision CAS、無 lost append 及 crash/retry。 |
| acceptance_surfaces | 2 | fix-standard 2 gate-spine 加 R-09/R-16/R-19，訊號值 5。 |
| spec_stability | 0 | accepted triad 完整且 planning completeness 無 blocker。 |
| orchestration | 2 | combo 9 cards、9 persona bindings。 |
| **合計** | **6 / Yellow** | **0+2+2+0+2=6**。 |

此 Yellow projection 僅適用單一 `engineering_outcome.py`。若要改其他 production module/schema，或 helper 重算為 Red，須回報真實分數並另拆，不能沿用本表或刪減 parent acceptance。

## Completion accounting

本票通過只證明 OutcomeStore local path/revision CAS；不證明 current WorkAuthority、#975 proof、remote closure、#976 Registry CAS、#977 integration、#962 aggregate、#887 parent 或 deployed runtime。
