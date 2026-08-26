---
status: accepted
work_item: phase2-closeout-reconcile
---

# Tasks

## 1. Provenance and RED

- [ ] 記錄 target `7ced8df0...` 與 #789/#790/#791 final heads、tree 缺口與
      dirty-root preservation boundary。
- [ ] 僅移植三支候選的具名 regression tests，在最新 `main` 觀察各能力因缺少
      production contract 而 RED。

## 2. Governed implementation

- [ ] 移植 #681 pinned Copilot wrapper/tree、metadata、install/rollback 與 attestation
      最小 delta；不得 broad PATH/HOME search。
- [ ] 移植 #695 generated-asset inventory、runtime compare 與 functional/comment-only
      drift classification；不得輸出 credential bytes。
- [ ] 移植 #716 production-shaped agent-loop probe、exact evidence binding 與
      no-fallback guards；保留現行 egress/sandbox derivation。
- [ ] 產出 `merge-summary.md`、`merge-risks.md`，逐項記錄 textual／behavioral／interface
      conflict 與 rollback。

## 3. Verification and delivery

- [ ] 三組 focused tests、cross-feature trust-root tests、完整 pytest、canonical OpenSpec、
      build/twine/clean-wheel smoke 與 actual PR-context preflight 全綠。
- [ ] 每條 standard/adversarial review finding 獨立驗證並修/駁/列管；修後重審。
- [ ] archive 本 change，提交 changelog fragment 與 `CHANGELOG.md [Unreleased]`。
- [ ] push PR、exact-head approval、CI、unresolved-thread audit 與 merge 全部完成。

## 4. Release and authority closeout

- [ ] bump 下一個 patch version，final-main exact-SHA RC qualification 通過後發布
      annotated tag、non-draft GitHub Release 與 hash-matching single wheel。
- [ ] 依 shipped evidence 關閉 #681/#695/#716 或將純 live-health 部分明確移至
      deployment-canary follow-up；修正 #789–#791 的 superseded 語意。
- [ ] 驗證 repo work-item/Todo、GitHub issue、release 與 `main` 對 Phase 2 狀態一致。
