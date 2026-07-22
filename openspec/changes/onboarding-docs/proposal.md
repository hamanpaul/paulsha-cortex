---
status: accepted
work_item: onboarding-docs
---

## Goals

把現行偏向專家維運者的 README 敘事，改寫為新手可依循的完整上手路徑，涵蓋安裝後到日常維運的生命週期文件（issue #94）。

## Why

deep research 指出 cortex 現有文件假設讀者已熟悉內部概念（systemd、spec/job/slice/work、control queue），新手缺乏循序漸進的入口；dogfood canary 累積的多個真實故障（F1 pipx 快照過期、F8 request timeout、F34 unit 指向已刪 venv 等）目前只存在於工程內部記錄，未轉譯成使用者可查的排除步驟。B8 補齊七份文件，一次補上入口與故障對照。

## What Changes

- 新增七份文件：`docs/onboarding/{quickstart,upgrade,rollback,troubleshooting,concepts,admin,runbook}.md`。
- README 新增「新手上手」導覽段，連結七份文件。
- 全文路徑一律 `~`/`$HOME`/環境變數/相對路徑，符合 R-21（`tier: shareable`）。
- 不改變任何既有命令行為；純文件變更。

## Capabilities

### New Capabilities

- `onboarding-documentation`: 新手上手到日常維運的完整文件契約——Quickstart/Upgrade/Rollback/Troubleshooting/Concepts/Admin/Runbook 七份文件，內容可回溯至 UX 規格與既有命令行為。
