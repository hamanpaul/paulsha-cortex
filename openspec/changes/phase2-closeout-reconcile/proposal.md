---
status: accepted
work_item: phase2-closeout-reconcile
---

## Why

Phase 2 已以 `v0.1.9` 發布，但 PR #789、#790、#791 被關閉為
`superseded by #794` 的治理紀錄與實際 tree 不一致：三支候選的關鍵
Copilot toolchain pinning、generated-vs-installed attestation 與 production-shaped
agent-loop qualification 程式／測試／規格並未進入 `v0.1.9` 的 exact commit。
只關閉 issue 或改 Todo 會把真實缺口藏掉，不能構成 closeout。

## Goals

- 從三支保留的 exact candidate head 重建可追溯的 governed merge，不重播陳舊的
  archive／restore 歷史。
- 將 #681、#695、#716 尚缺且仍適用的行為移植到最新 `main`，保留後續 Phase 2
  installer、sandbox、release qualification 加固。
- 以 RED→GREEN、完整 preflight、獨立 review、exact-main RC 與新 patch release
  證明 recovered code 真正 shipped。
- 修正 issue／Todo／PR closeout 語意；未被當前 evidence 滿足的 live rollout
  claim 必須明確留在 deployment canary，不得冒充 release evidence。

## Non-Goals

- 不重開或合併已關閉的 #789–#791。
- 不讀取或搬移 operator HOME credentials，不修改現行 production services。
- 不把 provider availability 或 live repository mutation 重新加入 deterministic
  release gate。
- 不以整檔覆蓋方式回退 `main` 在候選分支建立後新增的加固。

## What Changes

- 整合 immutable Copilot wrapper/tree、exact metadata 與 installed attestation。
- 整合完整 generated-asset inventory 與 functional/comment-only drift classification。
- 整合走 production launcher/template seam 的 agent-loop probe 與 fail-closed evidence。
- 新增 governed merge summary、risk/rollback 記錄與 Phase 2 closeout authority。
- 完成後 bump 下一個 patch version，重新跑 exact-main RC 並發布唯一權威 wheel。

## Capabilities

### Modified Capabilities

- `trust-root-phase2-closeout`: Phase 2 的正式完成宣稱必須與 shipped tree、issue
  authority、deterministic release evidence 和獨立 deployment-canary 邊界一致。

## Impact

- 主要影響 `paulsha_cortex/trust_root/permgen.py`、launcher／delivery／manager
  接縫、trust-root CLI/probe、對應 tests/specs/docs 與 release version metadata。
- 不新增外部依賴；不變更 secrets contract；不直接部署到 `/opt/cortex`。
