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
  `cortex/legacy-binding-checkpoint-receipt/v1`，不清 binding、不改歷史欄位。同步補上
  golden-vector / ABA / replay / rollback 測試。
