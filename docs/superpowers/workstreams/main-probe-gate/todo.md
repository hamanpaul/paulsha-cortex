---
status: accepted
work_item: main-probe-gate
domain_breadth: 0
state_consistency: 2
---

# Main probe gate todo

## Tasks

- [ ] 在 `work_bridge.py` 加 typed success/failure（含 optional `main_head`）、full commit SHA/object validation，以及 direct bounded subprocess probe；逐一保留 fetch/rev-parse/merge-base/merge-tree/parser 的 stage、returncode 或 timeout-null、error_kind，不使用 `base_sha_probe` 或無 timeout contract 的 default runner。
- [ ] 用 `merge-tree -z` 做 NUL-safe conflict path parse；測試包含含空白及換行的多條 path、CHANGELOG/top insert classification、README/multiple conflicts、clean-behind、M-preserving post-fetch failures、bad SHA、非 commit object、unsupported option、command timeout、preflight 間 main 前進、merged PR、terminal refresh。
- [ ] Bare-origin in-sync regression 讓兩次 probe 都見到 C 含 M，證明照常完成 preflight 並走到只寫本機 fixture 的 push path；behind/conflict/failure regression 證明該 tick 不 preflight、不 push、不建 PR、不 request Copilot。
- [ ] Integration regression：bare origin fetch failure → Manager `main-sync-unavailable` needs_human stop → 修復 fixture remote/ref → 使用 production operator workflow-resume path → ship validator 再次 probe，斷言第二個 fetch 及新的精確 M。此測試不取代 child 04 的 durable context read-back；不能只因非-define `_claim_action` 的 `workflow_starter` no-op 就 PASS。
- [ ] 更新對應 lifecycle docs、changelog fragment 與 `[Unreleased]`；跑 focused + full repository gates/network guard。

## Sizing inputs

單一 production module；兩次 probe 間 remote main 可前進，故 state consistency 2。使用 fix-standard 的固定 acceptance/orchestration dimensions；本 work item 自身三件 accepted artifacts 作 spec_stability=0 輸入。
