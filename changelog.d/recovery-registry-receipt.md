# 862 recovery-registry-receipt

- `JobRegistry` 現在依 recovery-registry-receipt 的 OpenSpec 契約實作 versioned
  recovery registry：fresh / checkpointed slice 會持久化
  `binding_version`／`binding_revision`，`prepare_recovery()` 與
  `commit_pre_candidate_recovery()` 以
  `cortex/recovery-registry-request/v1` digest、exact binding CAS、required-step
  receipts 與 idempotent replay 寫入 `cortex/recovery-registry-receipt/v1`；舊
  builder/reviewer job 會落 `cortex/job-supersession/v1`，而
  `record_job_consumption()` 另以 `cortex/job-consumption/v1` 保存獨立 completion
  proof。Legacy row 可透過 `checkpoint_legacy_binding()` 用 fingerprint +
  provenance 建立 revision 1 的
  `cortex/legacy-binding-checkpoint-receipt/v1`，不清 binding、不改歷史欄位；另補上
  pre-`bound_binding` job disposition persisted state 的 backward-compatible
  loader / replay 正規化，且 ordinary `repin_slice()` / `update_slice()` /
  `record_action()` binding bump 會在 drift 前先補寫缺失 witness，不需 schema bump。
  同步補上
  rename/fsync/rollback fault injection、checkpoint drift/replay 與 planning-gate
  regression，並更新 `docs/unified-work-lifecycle.md` 說明 recovery/checkpoint
  仍屬 registry-only contract、既有 CLI 不新增 public 動詞，另留 `docs/evidence/`
  的 pre-archive local validation record。
- `record_job_supersession()` 與 `record_job_consumption()` 現在接受省略 `at` 的相同
  disposition retry，保留第一次寫入的時間且不再持久化；相同明示時間的 replay
  也跳過寫入，明示不同時間仍回報 `request-content-conflict`。
