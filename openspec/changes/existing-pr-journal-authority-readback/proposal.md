---
status: draft
work_item: existing-pr-journal-authority-readback
issue: 1070
---

## Why

在 #1069 同 run Todo authority recovery/reverify 完成後，既有 Candidate/PR 的 delivery-journal row 仍需精確對齊新的 authority provenance。若 caller 重複寫入或把 journal row 當成可重建資料，會造成 duplicate transaction/event 或錯誤 PR 綁定。

## What Changes

- 以 #983 已 landed conditional-write API 讀回同一 run/Candidate/PR 的 durable journal row。
- 只在 exact row identity 與 durable revision 相符時，必要地同步 authority digest/vector；保留原 transaction result 與 PR identity。
- 重播、unknown、conflict 與 crash/re-entry 採 same-identity fail-closed recovery。
- 將 #1069 定義為 readonly journal identity/revision handoff；#1070 獨立擁有 existing-PR journal authority reconciliation。

## Capabilities

### New Capabilities

- existing-pr-journal-authority-readback：定義 same-run recovery 後，existing Candidate/PR journal row 的 exact authority read-back、conditional reconciliation 與 no-duplicate/no-remote-side-effect 邊界。

### Modified Capabilities

無。

## Impact

- 唯一預期 production module：paulsha_cortex/coordinator/work_actions.py。
- 消費 #983 已發布 API；不新增 journal writer/store、registry transition、GitHub API 或 schema。
- 測試、spec/design/todo、OpenSpec 與 changelog 可更新。
- 不 push、create/update PR、merge、close、改 Candidate 或執行 exact-D closeout。
