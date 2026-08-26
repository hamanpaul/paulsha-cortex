---
status: accepted
work_item: phase2-closeout-reconcile
---

## Why

Phase 2 已以 `v0.1.9` 發布，現行 installer 與 RC qualification 已取代 PR #789、#790
的舊設計；PR #796 建立的 deployment-canary lifecycle seam 則比 PR #791 完整，但尚未
機械證明指定 Codex model 在 `worktree-isolation` 自主執行命令。work-item／Todo 仍指向不存在的
OpenSpec 與過時 PR，形成錯誤 authority。獨立比對另發現 generated-vs-installed
attestation 會把 executable shebang 漂移誤判為 comment-only，並把 polkit 的獨立
註解誤判為 functional drift。對抗審查再證實 ambient planner/apply CLI 與新 plan 無法
承接舊 receipt provenance，v0.1.9→v0.1.10 的 production upgrade contract 並不存在；
這些 fail-open／false-positive／authority discontinuity 必須在正式收尾前修正。

## Goals

- 以 category-aware normalization 讓 shim／toolchain shebang 漂移 fail closed，polkit
  的獨立註解只產生 warning。
- 以 exact tree 與 RC evidence 記錄 #681、#695 已由現行架構取代，不回灌舊 branch。
- 強化 #716 deployment-canary：pin exact Codex builder，綁定 Manager-owned job spec，
  exact HEAD command 與 provider-persisted runtime identity；沒有 live success 前維持 open。
- 以 root-owned sealed candidate CLI 與明示 qualified prior receipt 建立可重播、可 rollback
  的 production upgrade path，不從 ambient HOME/PATH 推論 authority。
- 以 RED→GREEN、完整 preflight、雙向 review、exact-main RC 與新 patch release 交付修正。

## Non-Goals

- 不重開、合併或 cherry-pick 已關閉的 #789–#791。
- 不讀取或搬移 operator HOME credentials，不修改現行 production services。
- 不把 provider availability 或 live repository mutation 加入 deterministic release gate。
- 不新增第二套 `permgen` inventory、舊式 wrapper publisher 或 standalone agent-loop probe。

## What Changes

- 修正現行 install attestation 的 category-aware functional/comment normalization。
- 強化 protected full-dispatch closeout 與獨立 validator 的 exact identity／command
  observation contract；只輸出 bounded identity、hashes 與 booleans。
- Installer 新增 `--prior-receipt` exact-step provenance handoff、全 filesystem pre-sweep、
  host-global transaction lock 與 runbook maintenance lease；runbook 使用 actual=declared、
  逐 wheel hash-locked 的
  offline input 建 root-owned、tree-hash sealed candidate venv 完成 plan/apply，並在 stop 後以
  plan-bound lease token、每次唯一 receipt 與停服務前落盤的 plan-bound maintenance snapshot
  處理 failure/signal restore；helper 單獨失效時由原 token 繼續收斂，整個 shell hard-crash
  則只允許以停服務前原子封存的 root-only reviewed plan，在 fresh shell 執行 explicit
  exact-plan recovery 依 durable snapshot rollback/restore，不重建 immutable input／venv。
- 更新三個 workstream Todo、work-item links、runbook 與 GitHub issue authority。
- 新增 governed merge summary、risk/rollback 記錄，說明拒絕舊 branch 的理由。
- 將版本升至 `0.1.10`，重新跑 exact-main RC 並發布權威 wheel、完整
  install-input archive 與永久 qualification manifest，逐 asset 核對 GitHub REST digest。

## Capabilities

### Modified Capabilities

- `trust-root-phase2-closeout`: Phase 2 的正式完成宣稱必須與 shipped tree、issue
  authority、deterministic release evidence 和獨立 deployment-canary 邊界一致。

## Impact

- 主要影響 `paulsha_cortex/trust_root/install/{core,cli}.py` 的 attestation／upgrade authority、
  `qualification/{driver,validate,verify_bundle}.py` 的 evidence/artifact contract、對應
  tests/specs/docs 與 release version metadata。
- 不新增外部依賴；不變更 secrets contract；不直接部署到 `/opt/cortex`。
