---
status: accepted
work_item: phase2-closeout-reconcile
---

# Tasks

## 1. Provenance and RED

- [x] 記錄 target `7ced8df0...` 與 #789/#790/#791 final heads、能力比對與
      dirty-root preservation boundary。
- [x] 以現行 inventory fixture 新增 shim shebang、toolchain wrapper shebang 與 polkit
      standalone comment 三個回歸，確認最新 `main` 如預期 RED。

## 2. Governed implementation

- [ ] 在現行 installer 實作 category-aware functional normalization；不得把 shebang
      當註解，也不得把 polkit 獨立註解當功能內容。
- [ ] 記錄 #681/#695 的現行替代能力與 #716 deployment-canary 邊界，不新增平行 authority。
- [ ] 產出 `merge-summary.md`、`merge-risks.md`，逐項記錄 textual／behavioral／interface
      conflict 與 rollback。

## 3. Verification and delivery

- [ ] focused tests、cross-feature trust-root tests、完整 pytest、canonical OpenSpec、
      build/twine/clean-wheel smoke 與 actual PR-context preflight 全綠。
- [ ] 每條 standard/adversarial review finding 獨立驗證並修/駁/列管；修後重審。
- [ ] archive 本 change，提交 changelog fragment 與 `CHANGELOG.md [Unreleased]`。
- [ ] push PR、exact-head approval、CI、unresolved-thread audit 與 merge全部完成。

## 4. Release and authority closeout

- [ ] bump 下一個 patch version，final-main exact-SHA RC qualification 通過後發布
      annotated tag、non-draft GitHub Release 與 hash-matching single wheel。
- [ ] 依 shipped evidence 關閉 #681/#695；將 #716 明確改列為 deployment-canary
      follow-up；修正 #789–#791 的 superseded 語意。
- [ ] 驗證 repo work-item/Todo、GitHub issue、release 與 `main` 對 Phase 2 狀態一致。
