---
status: accepted
work_item: phase2-closeout-reconcile
---

## Why

Phase 2 已以 `v0.1.9` 發布，現行 installer、RC qualification 與 deployment canary
也已取代 PR #789、#790、#791 的舊設計；但 work-item／Todo 仍指向不存在的
OpenSpec 與過時 PR，形成錯誤 authority。獨立比對另發現 generated-vs-installed
attestation 會把 executable shebang 漂移誤判為 comment-only，並把 polkit 的獨立
註解誤判為 functional drift。這個 fail-open／false-positive 缺口必須在正式收尾前修正。

## Goals

- 以 category-aware normalization 讓 shim／toolchain shebang 漂移 fail closed，polkit
  的獨立註解只產生 warning。
- 以 exact tree 與 RC evidence 記錄 #681、#695 已由現行架構取代，不回灌舊 branch。
- 將 #716 明確改列為發布後 deployment-canary 驗收，不再冒充 package release blocker。
- 以 RED→GREEN、完整 preflight、雙向 review、exact-main RC 與新 patch release 交付修正。

## Non-Goals

- 不重開、合併或 cherry-pick 已關閉的 #789–#791。
- 不讀取或搬移 operator HOME credentials，不修改現行 production services。
- 不把 provider availability 或 live repository mutation 加入 deterministic release gate。
- 不新增第二套 `permgen` inventory、舊式 wrapper publisher 或 standalone agent-loop probe。

## What Changes

- 修正現行 install attestation 的 category-aware functional/comment normalization。
- 更新三個 workstream Todo、work-item links、runbook 與 GitHub issue authority。
- 新增 governed merge summary、risk/rollback 記錄，說明拒絕舊 branch 的理由。
- 完成後 bump 下一個 patch version，重新跑 exact-main RC 並發布唯一權威 wheel。

## Capabilities

### Modified Capabilities

- `trust-root-phase2-closeout`: Phase 2 的正式完成宣稱必須與 shipped tree、issue
  authority、deterministic release evidence 和獨立 deployment-canary 邊界一致。

## Impact

- 主要影響 `paulsha_cortex/trust_root/install/core.py` 的 attestation normalization、
  對應 tests/specs/docs 與 release version metadata。
- 不新增外部依賴；不變更 secrets contract；不直接部署到 `/opt/cortex`。
