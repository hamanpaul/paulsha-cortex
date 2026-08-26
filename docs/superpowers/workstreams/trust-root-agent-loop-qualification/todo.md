---
status: accepted
work_item: trust-root-agent-loop-qualification
---

# Trust-root agent-loop deployment-canary follow-up

Issue: `hamanpaul/paulsha-cortex#716`.

## Reclassification

舊 PR #791 的 standalone probe 沒有走完整 production intake／per-job clone／bundle／
workflow terminal，不能作為 live acceptance，亦不得回灌。PR #796 已建立較強且唯一的
canonical seam：protected `Deployment canary` 以 `_full_dispatch()` 執行真實
`cortex work intake` 到 terminal，並驗證 provider/model/quota、Manager GitHub、claim→ship、
canonical evidence、commit bundle 與 gates。

這是**發布後 deployment acceptance**，不是 Phase 2 source/package release blocker。
Deterministic RC 不讀 secrets、不呼叫 provider、不修改外部 repository；package release
成功也不能推論當下 production/provider 健康。

## Acceptance

- [x] production-shaped live acceptance 已移到獨立 protected deployment-canary workflow。
- [x] SKIP、fallback、quota、model mismatch、ref drift 或未 terminal 均 fail closed。
- [x] work-item authority 已移除較弱的 PR #791 與不存在的 OpenSpec 指向。
- [ ] 在受保護 environment 具備明示的 4 secrets、3 variables，且 operator 授權目標
      repository mutation 後，對指定 deployed SHA 跑成功 canary；完成前 #716 保持 open。

目前沒有成功 canary run，因此 production/provider live health 仍是**未證實**；這不回頭
否定已通過 deterministic gates 的 Phase 2 source/package release。
