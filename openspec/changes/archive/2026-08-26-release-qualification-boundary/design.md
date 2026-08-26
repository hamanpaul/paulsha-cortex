---
status: accepted
work_item: release-qualification-boundary
---

## Context

目前 manual RC workflow 在 privileged systemd container 內先驗 exact wheel/bundle，再匯入四份
protected secrets，呼叫三個 provider、執行 Manager GitHub dry-run，最後對真實 repository 做完整
intake-to-closeout。release workflow 只接受同一 default-branch SHA 的成功 RC evidence。

這兩個條件無法同時穩定成立：若 full dispatch 產生並合入 PR，default branch 由 A 前進到 B，
則綁定 A 的 RC evidence 立即不符合 release 的 `main == candidate SHA` gate。即使 probe 沒有合入
本 repo，release 仍被第三方 login、quota、network 與 mutable repository state 決定，這些都不是
wheel 是否可安全安裝的屬性。

## Decisions

### 1. Evidence schema v2 明列不可互換的 profile

root 新增 `profile`，唯一允許值為 `release` 或 `deployment-canary`，並將 `schema_version` 升為 2。
Python validator 依 profile 套用不同 required tests/artifacts/provider semantics；CLI 以互斥的
`--require-release-profile` 與 `--require-canary-profile` 鎖定呼叫端意圖。release workflow 必須使用
前者，不能接受 canary evidence；canary workflow 必須使用後者，不能以缺少 live evidence 通過。

### 2. Release profile 只驗 candidate 可控制的 deterministic invariant

release profile 保留 exact wheel/bundle/image binding、fresh/idempotent/drift/rollback/reinstall、
selfcheck、registry equation、generated-installed attestation、service identity/hardening、五族 attack
matrix 與 negative controls。它不呼叫 provider CLI、不讀 GitHub auth、不執行 Manager remote probe，
也不進入 `cortex work intake`。

installer 的 required credential contract 仍透過固定 `{}` bytes 走正式 stdin import adapter；這些
fixture 不是有效登入資料，不取自環境，也不可能授權外部操作。release profile 啟動服務後只執行
本機驗證與權限攻擊矩陣。evidence 的 `providers` 必須是空陣列，且不得列出 provider、dispatch 或
Manager GitHub artifacts/tests。

### 3. Deployment canary 保留 live flow，但永不成為 package release prerequisite

新增 manual `deployment-canary.yml`，在 protected `rc-qualification` environment 中接收既有四份
secrets 與三個 probe variables，呼叫同一 runner 的 `deployment-canary` profile。它保留 native
provider preflight/smoke、Manager GitHub dry-run 與 full dispatch closeout，並產生獨立 artifact
名稱。它可以用來判斷特定部署環境是否健康，但 release workflow 不查詢、不下載也不驗證該 run。

### 4. Redaction 依 profile fail closed

release profile 只做 credential/token pattern 掃描，且若 workflow 或 runner 引用任何
`CORTEX_RC_*` live input，contract tests 直接失敗。deployment-canary profile 除 token pattern 外，
仍要求四份 secret env 都存在，並逐值掃描輸出；缺值即失敗。

### 5. Release exact-SHA join 保持不變，只更正被 join 的證據

release workflow 仍要求 default-branch HEAD、Tests、final-head approval/checks、fresh RC run、wheel
hash 與 candidate SHA 完全一致。唯一變更是 qualification validator 要求 `release` profile。
如此 RC 不再改變 GitHub remote state，A 的成功 evidence 能合法解鎖 A，不會自行製造 B。

## Safety and Failure Semantics

- profile 缺失、未知、與 validator flag 不符或混入另一 profile 的 artifacts/tests/providers，一律失敗。
- release profile 不接受 environment secrets 或 probe variables；synthetic fixtures 固定於 runner，
  只經 stdin 傳入 container tmpfs，使用後立即移除。
- canary 仍不得輸出 credential bytes；secret 缺失、provider fallback、quota/login/model mismatch、
  remote ref drift 或 dispatch 未 terminal 都失敗。
- 兩條 workflow 均只允許 `workflow_dispatch` 並維持 40-hex action pins。

## Rollout

1. 先以 workflow/validator/runner regression 建立 RED，證明舊 RC 仍依賴 secrets 與 external writes。
2. 實作 profile split，跑 focused/full tests、OpenSpec strict validation 與 policy preflight。
3. 合入後在 exact default-branch SHA 執行 deterministic RC qualification。
4. 只在該 evidence 與 release preflight 全綠後建立 `v0.1.9` tag/GitHub Release。
5. deployment canary 可另行手動執行；其成功與否不改變 `v0.1.9` package release 判定。

## Risks / Trade-offs

- [Risk] package release 不再證明第三方 provider 當下可用。→ 這是 deployment canary 的責任；release
  notes 與證據必須精確區分 installed-system qualification 與 live deployment health。
- [Risk] synthetic credential 可能只驗到 import plumbing，未驗 credential 語意。→ canary 保留 native
  auth/quota/model 驗證；release profile 明確不做 live-auth 宣稱。
- [Risk] schema v2 使既有 v1 evidence 不能重用。→ fail closed 是刻意行為；重新對 exact SHA 跑新版
  deterministic workflow，避免舊 live evidence 被誤認為新 release profile。

## Open Questions

無。
