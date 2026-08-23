---
status: accepted
work_item: manager-gitconfig-delivery
---

# Tasks

- [x] 1.1 RED：依 `docs/superpowers/plans/manager-gitconfig-delivery.md` 新增可重現 regression coverage，先鎖住 Manager `credential.https://github.com.helper` 缺口與 Git 對 GitHub HTTPS helper 的解析缺口，確認失敗。
- [x] 1.2 實作至 GREEN：由 trust-root authority 產生 Manager helper 設定並完成 dry-run credential lookup；`recover-repair-commit` 改經授權 ledger/handoff 取得 builder-owned HEAD evidence，而非由 Manager 讀 builder worktree。
- [x] 1.3 `changelog.d/manager-gitconfig-delivery.md` fragment 與 `CHANGELOG.md [Unreleased]` entry（#763）。
- [x] 1.4 依 builder/retry-build 邊界重整 active OpenSpec tasks：只描述並勾選 pre-archive 的 implementation / gate 工作，不提前宣稱 archive、delivery、merge、issue closure 或 done。
- [x] 1.5 重跑 builder 可完成的驗證：focused regression tests、`python3 -m pytest -q` 全套、帶 PR 上下文的 `policy_check`，以及 authoritative preflight（metadata mode）。
- [x] 1.6 以 conventional commit 提交 tested descendant candidate（僅涵蓋 pre-archive builder repair）。

## 驗收

Manager 的 generated `.gitconfig` 具備 GitHub HTTPS credential helper，且 Git 對 `https://github.com` 的 lookup 可由該設定解析；helper 不擴寬到其他 host。後續修法完成後，Manager 的 dry-run credential lookup 不暴露憑證、不改動 remote ref；`recover-repair-commit` 則改走授權 handoff/ledger，不再直接讀 builder 樹。
