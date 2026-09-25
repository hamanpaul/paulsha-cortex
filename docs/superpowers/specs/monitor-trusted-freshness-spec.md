---
status: draft
work_item: monitor-trusted-freshness
issue: 1078
---

# Monitor trusted freshness 規格

## Requirements

### R1 — Bind exact inputs to one attempt

對每個 repo成功 correlation，MUST 記錄 parser實際消費的 `.cortex/work-items.yaml` raw-byte SHA-256（缺檔使用穩定 absent revision）、本 generation使用的 provider revisions，以及 `source_id → revision` map。不得以另一次 scan/read冒充本次 input。

### R2 — Verify durable snapshot before success

Monitor MUST 先 durable-write candidate `WorkSnapshot`，再從 store reload並核對 canonical digest、sequence、target rows與 provider/source revisions。只有 read-back吻合後，才可把 #1077所建立的同一 generation標為 succeeded並綁定 manifest。

### R3 — One read-only trusted freshness API

提供唯讀 repo/work freshness API，從 Monitor canonical configured root重讀目前 input，核對最新成功 generation、manifest、snapshot read-back與配置的 age bound。它不接受 caller自報 root/hash/revision。

### R4 — Fail closed

Missing/legacy/unknown/running/failed/stale/malformed/mismatched marker、input、source或snapshot以及 I/O error均回傳 typed untrusted reason。last-good rows僅供診斷，不能代替成功 evidence。

### R5 — Slice boundary

本票依賴 #1077 durable attempt ledger與 #1063 source/path contract；只交付 success evidence publication與 Monitor API，不改 WorkAuthority reader（#1065）或 Manager admission（#1054）。
