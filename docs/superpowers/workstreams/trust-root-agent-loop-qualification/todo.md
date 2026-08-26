---
status: accepted
work_item: trust-root-agent-loop-qualification
---

# Trust-root agent-loop deployment-canary follow-up

Issue: `hamanpaul/paulsha-cortex#716`.

## Reclassification

舊 PR #791 的 standalone probe 沒有走完整 production intake／per-job clone／bundle／
workflow terminal，且 prompt 直接指定 shell commands、producer 不解析 command event，
不能作為 live acceptance，亦不得回灌。PR #796 建立 protected `Deployment canary` 的
intake-to-terminal lifecycle seam，但原始 closeout 沒 pin builder identity，也沒證明
`worktree-isolation` 的 Codex command event；本 closeout 才補齊 exact run override、typed
runtime、Manager-owned job spec 與 hash-only live observation。

這是**發布後 deployment acceptance**，不是 Phase 2 source/package release blocker。
Deterministic RC 不讀 secrets、不呼叫 provider、不修改外部 repository；package release
成功也不能推論當下 production/provider 健康。

## Acceptance

- [x] production-shaped live acceptance 已移到獨立 protected deployment-canary workflow。
- [x] SKIP、fallback、quota、model mismatch、ref drift 或未 terminal 均 fail closed。
- [x] canary 固定 `codex/gpt-5.3-codex-spark`，並綁定 resolved identity、builder runtime
      與 Manager-owned template spec。
- [x] 只有唯一 `worktree-isolation` job 的 completed／exit 0／non-empty-output
      `command_execution` 可產生 agent-loop marker；raw command/output 不進 evidence。
- [x] job JSONL 明列為 observational telemetry，不冒充獨立 authority。
- [x] work-item authority 已移除較弱的 PR #791 與不存在的 OpenSpec 指向。
- [ ] 在受保護 environment 具備明示的 4 secrets、3 variables，且 operator 授權目標
      repository mutation 後，對指定 deployed SHA 跑成功 canary；完成前 #716 保持 open。

目前沒有成功 canary run，因此 production/provider live health 與 #716 acceptance 仍是
**未證實**；這不回頭否定已通過 deterministic gates 的 Phase 2 source/package release。
