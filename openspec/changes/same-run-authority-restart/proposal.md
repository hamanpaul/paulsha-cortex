---
status: draft
work_item: same-run-authority-restart
issue: 1069
---

# 既有 Candidate run authority restart 提案（#1069）

## Why

當唯一 Todo authority 前進後，已有 Candidate 與 open PR 的 run 必須只在 operator 明確恢復後，沿同一 WorkflowRun 做 exact authority restart，並針對原 Candidate/PR 重跑 verify/review。自動 claim、模糊 source selection、跨 claim-era 證據或缺少 PR/journal read-back 都會把不相干的證據錯認為新 gate。

## What Changes

此 work item 由 #1069 唯一擁有，`work_id` 與 OpenSpec change 均為 `same-run-authority-restart`。production scope 預期只有 `paulsha_cortex/coordinator/work_actions.py`：讓既有 operator `work resume` 在明確 fresh WorkAuthority 下，先讀回 exact run/registry/journal/PR tuple，再消費 #1068 的 single exact-CAS transition，CAS 後重新確認 authority、Candidate、PR/head 和 jobs，最後僅派 verify/review。

## Requirements

- #1054 及 #1063/#1064/#1065 是 Todo qualification、successful Monitor generation 與 WorkAuthority freshness 的硬前置；#1068（依賴已 merge 的 #966）是 registry transition hard dependency。前置未 landed 不得 intake。
- recovery 只能由 operator 對 exact repo/work ID 明確 resume；periodic scan/automatic claim/start/intake 不得 reset/re-dispatch。
- 唯一 Todo、source provenance、完整 revisions/snapshot/provider/digest 都須在 reset 前後 fresh 讀回；ambiguous/stale/drift typed fail closed。
- 精確保留同一 run/Candidate/PR，active job 必須為零；舊 build 和舊證據保留，但 verify/review gate 因新 authority 失效並重跑。
- Delivery journal 僅唯讀核對既有 row identity/revision，禁止 journal writes、PR push/create/update/merge/close 或任何 delivery mutation。#1070 負責完整 existing-PR journal authority read-back/conditional delivery，消費 #983 writer。
- #983 run/Candidate/PR 數值只用 fixture；GitHub/journal/delivery write spies 必須為零。不得操作正式 #983 run/PR #1049、abandon/recover-pre-candidate、supersede/retire、建替代 run。
- 詳細 acceptance invariants、design decisions 與九張任務卡分別見 own OpenSpec spec/design/tasks 及 Superpowers spec/design/Todo；各 view 都以同一唯一 owner/work ID 為 binding。

## Out of Scope

本提案不實作 registry primitive/writer、delivery journal writer、source resolver/path scanner/Todo parser、CLI 新命令或 GitHub mutation，不處理 merged PR completion、main-sync、main conflict 或 #983 delivery writer。任何第二 production module 或未發布 cross-writer API 都要求 issue-backed re-scope。

## Readiness

此 change 與 linked Superpowers artifacts 目前均為 `draft`，須 root exact-head review/acceptance 後才成為正式 planning authority。Issue 原 sizing 6/Yellow 是 accepted-only projection；本 draft bundle 官方 helper 的實際結果是 8/Red。PR merge 和 formal intake 不是同一狀態。
