---
status: accepted
work_item: phase2-install-docker-qualification
---

## Why

Trust-root Phase 2 的四分 UID、權限產生器與實機攻擊矩陣已有成功證據，但目前沒有可從
候選 wheel 重建、可驗證、可回滾的 privileged install flow。現有
`cortex install service` 仍只安裝 user-systemd unit；release workflow 也只證明 wheel
可安裝，沒有證明 systemd、polkit、ACL、多 UID、憑證交付與完整派工在 exact candidate
上成立。

因此 `v0.2.0` 必須先建立一條 fail-closed 的 trust-root installer 與 Ubuntu 24.04
systemd Docker qualification gate。qualification 是 release prerequisite，不是 production
support 宣告；在 exact-SHA qualification 全綠前不得建立 tag 或 GitHub Release。

## Goals

- 提供 `plan`、`apply`、`credentials import`、`activate`、`verify`、`rollback` 六個明確的
  trust-root install 階段，並保留 Phase 1 `cortex install service` 相容入口。
- 以 hash-bound plan、root-owned receipt、可 replay transaction、generated-vs-installed
  attestation 與顯式 credential import，讓安裝／升級／回滾可重建且不洩漏 secret。
- 用同一 exact candidate wheel/bundle 在 Ubuntu 24.04 systemd container 執行 fresh、
  idempotency、drift、rollback、攻擊矩陣、provider smoke 與完整派工 qualification。
- release workflow 必須驗證同 commit 的成功 RC qualification evidence，否則 fail closed。

## Non-Goals

- 不把 Docker 宣告成 Cortex production support target。
- 不自動探索 operator HOME 的憑證，不自動 sudo，不掛 host Docker socket／HOME。
- 不在一般 PR workflow 執行昂貴 provider/full-system qualification。
- 本 change 不發布 PyPI；production reinstall 仍需另一次明確授權。

## Capabilities

### New Capabilities

- `trust-root-installation`: 四分 UID trust-root 的 plan/apply/import/activate/verify/rollback
  契約與 receipt authority。

### Modified Capabilities

- `release-engineering-pipeline`: GitHub Release 必須由同一 exact commit 的成功 RC Docker
  qualification evidence 解鎖。

## Impact

- 新增 trust-root installer/transaction/schema、公開 CLI、單元與整合測試。
- 新增 Ubuntu 24.04 systemd reference image、local harness、manual RC workflow 與 redacted
  `qualification.json` evidence。
- 修改 release workflow，使 release 建立前驗 candidate SHA、wheel/bundle/image digest 與
  qualification freshness。
- 正常化合入 runtime credential harvest (`c35516e`) 與 verification/Copilot verdict
  (`98978b6`) 的最小 prerequisite commits；不整包採用 live-closeout branches。
