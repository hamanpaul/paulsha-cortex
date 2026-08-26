---
status: accepted
work_item: phase2-closeout-reconcile
---

## Context

三支候選都從舊 baseline 分岔，之後 `main` 已建立 transactional installer、root-owned
hash-bound toolchain、完整 install inventory、production full-dispatch canary 與 exact-SHA
RC/release。逐項行為比對證明現行架構已覆蓋舊分支的有效目的，舊 API 本身不是交付
契約；直接 merge 反而會引入非原子 reinstall、第二套 inventory authority 與較弱 probe。

## Decisions

### 1. 使用 governed reconciliation，不做 branch replay

以 `7ced8df0a24c55c49ee894b3118ea18d2a97b552` 為 target，三個 PR final head
只作 provenance source。依「能力是否存在且更強」判斷 supersession，不以舊 module／test
檔名是否存在判斷。#789 的手動 publisher、#790 的第二套 inventory、#791 的 standalone
probe 均拒絕回灌；只修正比對中證實仍存在的現行 attestation normalization 缺陷。

### 2. Attestation normalization 依 artifact category 判定

- `shim` 與 `toolchain_wrappers`：`#!` 決定 interpreter，必須保留為 functional line；
  其他獨立 shell/Python 註解可忽略。
- `polkit`：獨立 `//` 與 `/* ... */` 註解可忽略；規則內容仍逐行比較。
- 其他 category 維持既有 `#`／`;` comment semantics，避免擴大行為變更。

三個 focused regression 分別鎖住兩種 shebang fail-closed 與 polkit comment-only warning。

### 3. Release qualification 與 deployment canary 邊界保持不變

Deterministic RC 只證明 artifact install/systemd/attestation/attack matrix，且不得取用
live credentials。需要 provider 的 agent-loop live execution 只屬 deployment canary。
現行 `_full_dispatch()` 已取代較弱的 standalone probe；「此刻 provider/live rollout
健康」仍需受保護環境的 canary evidence，不能由 package release 推論。

### 4. 修正必須以新 immutable patch release 交付

`v0.1.9` 保持不可變歷史；本 change 合併後以新 patch version 重新產生 exact-main RC、
annotated tag、GitHub Release 與唯一 wheel。#681/#695 依 shipped replacement evidence 關閉；
#716 保持 open，但改列為 deployment-canary acceptance，不再阻擋 Phase 2 source/package。

## Risks / Trade-offs

- normalization 若過度忽略內容會造成 fail-open；只忽略 category 明確定義的獨立註解，
  shebang 與 inline code 一律保留。
- 舊 branch 的具名檔案不存在不代表能力不存在；merge summary 必須列出現行替代 call path
  與拒絕移植的風險。
- agent-loop package code 的存在不等於 live provider 成功；docs、issue comment 與
  qualification profile 必須維持這個誠實邊界。

## Rollback

整併以獨立 feature branch／PR 交付；合併前可直接丟棄該 worktree。合併後若發現回歸，
revert closeout merge commit；新 tag 只能在 exact-main RC 通過後建立，不覆寫 `v0.1.9`。
