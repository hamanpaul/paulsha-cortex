---
status: accepted
work_item: phase2-closeout-reconcile
---

## Context

三支候選都從舊 baseline `1022d77f9f9a212f9a1e09c1c0698643ee4f581d`
分岔，之後 `main` 已累積 installer、runtime isolation、RC/release 等大量加固。
直接 merge 或整檔採用任一候選的 `permgen.py` 會同時帶回舊行為；另一方面，只用
`v0.1.9` RC 綠燈推論三支候選已整合也不成立，因為 tree 中缺少其具名測試與模組。

## Decisions

### 1. 使用 governed semantic transplant，不做 branch replay

以 `7ced8df0a24c55c49ee894b3118ea18d2a97b552` 為 target，三個 PR final
head 為 provenance source。先移植各自 RED tests 並在 target 上觀察預期失敗，再按
function／contract 移植最小 production delta。反覆 archive／restore、過時 work-item
link 與舊 changelog 不重播。

### 2. 三項能力分開驗證，共用一個 closeout release

- #681：wrapper 必須只 exec root-owned pinned payload，拒絕 PATH/HOME search、
  symlink/path escape 與 metadata mismatch。
- #695：由 canonical renderer 建 inventory；functional drift fail、comment-only
  drift warn，且 credential surface 只留 metadata/hash。
- #716：probe 必須經 production `SubprocessLauncher`、job spec、generated template
  與 systemd start seam，對 fallback/SKIP/quota/model mismatch fail closed。

整合測試與規格各自保留，避免一個總體綠燈遮住單一能力未落地。

### 3. Release qualification 與 deployment canary 邊界保持不變

deterministic RC 只證明 artifact install/systemd/attestation/attack matrix，且不得取用
live credentials。需要 provider 的 agent-loop live execution 只屬 deployment canary。
source harness 可在 release 中交付，但「此刻 provider/live rollout 健康」仍需另一次
canary evidence，不能由 package release 推論。

### 4. 完成宣稱必須在 recovered code 的新 release 上

`v0.1.9` 保持不可變歷史；本 change 合併後以新 patch version 重新產生 exact-main
RC、annotated tag、GitHub Release 與唯一 wheel。issues 只在各自 acceptance 與 shipped
evidence 對齊後關閉；若只剩環境健康，則在 closeout comment 明確移到 canary，而非保留
「Phase 2 source blocker」語意。

## Risks / Trade-offs

- 三來源都修改 `permgen.py`，存在行為與文字衝突；以現行 registry/invariant 為主，
  逐項移植並跑交叉 focused tests。
- source branch 測試可能依賴已演進的 fixture；只修 fixture compatibility，不降低 assertion。
- agent-loop source harness 的存在不等於 live provider 成功；docs、issue comment 與
  qualification profile 必須維持這個誠實邊界。

## Rollback

整併以獨立 feature branch／PR 交付；合併前可直接丟棄該 worktree。合併後若發現回歸，
revert closeout merge commit；新 tag 只能在 exact-main RC 通過後建立，不覆寫 `v0.1.9`。
