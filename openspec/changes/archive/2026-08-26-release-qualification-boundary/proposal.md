---
status: accepted
work_item: release-qualification-boundary
---

## Why

現有 `rc-qualification` 同時承擔兩種互相衝突的責任：一方面要對 exact candidate
wheel、systemd installer、權限與 rollback 提供可重現的 release 證據；另一方面又要求
四份真實 provider/GitHub credentials，並在 probe repository 執行完整 intake-to-merge。
完整派工會改變 GitHub default branch，而 release 又要求 qualification SHA 等於當下
default-branch HEAD，因此成功的 live probe 本身即可使 candidate 過期。這讓 release gate
依賴外部配額、真實憑證與可變 remote state，也形成無法穩定閉合的 SHA 循環。

## Goals

- 將 release-blocking qualification 收斂為 exact-SHA、無真實 credentials、無外部寫入且可重現的
  Ubuntu 24.04 systemd/container 驗證。
- 保留 installer credential import/activation plumbing，但只使用固定、非秘密的合成 fixture。
- 將真實 provider、Manager GitHub 與 intake-to-closeout 驗證拆成受保護、手動執行的
  deployment canary；其結果不得阻擋 package release。
- 以 profile-aware evidence schema 與 validator 防止兩種證據互相冒充。

## Non-Goals

- 不降低 fresh install、idempotency、drift、rollback、reinstall、service identity、generated-vs-installed
  attestation 或五族 attack matrix 的 release gate 強度。
- 不將 deployment canary 自動化為 production deployment，也不替 operator 建立或上傳 credentials。
- 不以 canary 成功宣稱所有 provider、配額或第三方服務永久可用。

## What Changes

- qualification evidence 升為 schema v2，明列不可互換的 `release` 與
  `deployment-canary` profiles。
- `rc-qualification` 移除 protected environment、live secrets 與 probe variables，
  只執行 deterministic installer/systemd/attestation/attack gates。
- 新增受保護的 manual deployment canary，承接 provider、Manager GitHub 與 full-dispatch
  驗證，但 release workflow 不查詢其結果。
- release validator 只接受 exact-SHA `release` profile，並拒絕 canary tests/artifacts。

## Capabilities

### New Capabilities

無。

### Modified Capabilities

- `release-engineering-pipeline`: exact-SHA release qualification 必須 deterministic、credential-free、
  external-write-free；live deployment canary 必須與 release gate 分離。

## Impact

- 修改 `qualification/run.sh`、`driver.py`、`validate.py`、evidence schema 與 redaction scanner，加入
  `release`／`deployment-canary` profiles。
- 修改 `.github/workflows/rc-qualification.yml`，移除 protected environment、secrets 與 probe variables。
- 新增 `.github/workflows/deployment-canary.yml`，保留既有 live provider/GitHub/full-dispatch 驗證。
- 修改 `.github/workflows/release.yml`，只接受 exact-SHA `release` profile evidence。
- 擴充 workflow、validator、runner 與 redaction regression tests。
