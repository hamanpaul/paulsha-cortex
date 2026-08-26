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

- [x] 在現行 installer 實作 category-aware functional normalization；不得把 shebang
      當註解，也不得把 polkit 獨立註解當功能內容。
- [x] 記錄 #681/#695 的現行替代能力與 #716 deployment-canary 邊界，不新增平行 authority。
- [x] 產出 `merge-summary.md`、`merge-risks.md`，逐項記錄 textual／behavioral／interface
      conflict 與 rollback。

## 3. Verification and delivery

- [x] focused tests、cross-feature trust-root tests、完整 pytest、canonical OpenSpec 與
      build/twine/clean-wheel smoke 全綠。
- [x] 每條 standard/adversarial review finding 已獨立驗證並修/駁/列管；修後回歸全綠。
- [x] 以官方 OpenSpec CLI archive 本 change，並提交 changelog fragment 與
      `CHANGELOG.md [Unreleased]`。
- [x] 建立 zh-TW PR、套用 release label，並以該 PR 的 actual metadata 跑 policy preflight。

## 4. Release-ready implementation

- [x] 將 `VERSION` 升至 `0.1.10`；release workflow 只接受 final-main exact-SHA RC，並發布
      annotated tag、non-draft GitHub Release、hash-matching single wheel、完整
      install-input archive 與 permanent qualification manifest，逐 asset 核對 REST digest。
- [x] 固化 authority closeout 邊界：#681/#695 只在 shipped evidence 後關閉；#716 明確保留為
      deployment-canary follow-up；#789–#791 不再作為平行 implementation authority。

## Post-merge operational closeout

下列動作依本 change 已驗證的 workflow 執行，屬 merge 後外部狀態，不作為「先 archive 才能
建立 PR」的循環 task gate：等待 final-head CI／review、merge；對 final-main SHA 跑 RC 並發布
0.1.10；核對三份 release asset REST digest；依 shipped evidence 關閉 #681/#695、保留 #716，
最後對帳 repo work-item／GitHub issue／release／`main`。其證據留在 PR、Actions、Release 與
issue timeline，不預先勾成已完成。
