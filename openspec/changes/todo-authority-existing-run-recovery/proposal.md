---
status: draft
work_item: todo-authority-existing-run-recovery
issue: 1055
---

# Todo Authority 前進後 Existing Candidate／PR Recovery

## Goals

為已有 Candidate 與 PR 的 Manager run 凍結安全恢復契約，避免新 canonical Todo authority 到位後舊 claim/evidence 繼續被錯誤採信，或因 recovery 重複建立 job、push、PR、merge、journal row。

主要 regression shape 是 #983 的既有 run、Candidate 及 PR #1049。整份契約保留 #1055 acceptance，不包含 live 操作。

## Hard prerequisites

- #966、#1054 與 #1063/#1064/#1065 實際合併至 main；執行前重新讀取三張 prerequisite tickets、#1054、accepted contracts 與 exact main revisions。
- #966 提供 durable JobRegistry raw-revision CAS；其中 #1063 提供 Todo semantic provenance/Tasks qualification 與 existing path guard；#1064 提供 trusted Monitor correlation generation/input watermark；#1065 提供 fresh-generation WorkAuthority reader。#1054 另提供 Manager admission/diagnostic、first-Builder exact run/claim digest 比對及 post-Todo pre-Candidate continuation contract。
- #983 的 durable conditional-write 和 identity-preserving journal read-back 已合併，或 fresh audit 證明 main 有完全等價 accepted contract。
- #1049 main conflict 由 #972/#973 處理；merged/closed PR 由 #962/#975/#976/#977 處理；merge 後 Todo closure 由 #810 處理。

Draft #1054 PR 或尚未確認的 source semantics 不算 prerequisite 已滿足。Red scope 的 child issues 已建立：#1068（blocked by #966 與 #1063/#1064/#1065/#1054）→ #1069（blocked by #1068 與 #1054 package）→ #1070（blocked by #983、#1068/#1069 與 #1054 package）。各票未完成 accepted triad/sizing/assignment 前均不可 intake。

## What Changes

- 凍結 old/new WorkAuthority 完整 source revision vector 與官方 digest；registry transition 同時 compare exact durable registry revision、run/claim/source/Candidate/gate/job/PR tuple。
- 只對既有 ongoing run、既有 Candidate、單一 exact open PR、no active job 的 explicit operator resume 做同 run verify/review reset。
- 只 invalidate verify/review gate，保留 Candidate、build history、舊 claim-era Job/evidence 與 PR identity。
- 用 landed #983 conditional journal write contract做 same-identity idempotent read-back；任何 unknown/conflict fail closed，沒有 push/create/merge/false closure。
- 為 reset、job dispatch、journal commit 與 read-back crash boundaries 凍結重啟語意，並以 #983 run shape 建立 fixture-only regression。

## Non-goals

No pre-Candidate abandon/recover-pre-candidate path; no automatic replacement run; no edit to live #983 run or PR #1049; no PR conflict resolution; no merge/closed PR completion; no post-merge Todo checkbox closure; no new journal writer or direct source revision patch.
