---
status: accepted
work_item: onboarding-docs
---

# onboarding-docs Plan

## Tasks

### 1. 內容盤點與草稿

- [ ] 逐份列出七份文件的大綱與引用來源（UX 規格章節 / issue #94 / dogfood F-finding 編號：F1、F8、F34 等），自我檢查有無杜撰尚未落地的行為，確認大綱涵蓋 issue #94 驗收條件。

### 2. 撰寫

- [ ] 撰寫 `docs/onboarding/quickstart.md`：pipx install → `cortex bootstrap` → 第一個 workflow，目標 10 分鐘內完成。
- [ ] 撰寫 `docs/onboarding/upgrade.md` 與 `docs/onboarding/rollback.md`：對齊 F1 pipx 快照過期路徑。
- [ ] 撰寫 `docs/onboarding/troubleshooting.md`：manager degraded、request timeout（F8）、systemd 不可用、executor 未登入、unit 指向過期 venv（F34）對照排除步驟。
- [ ] 撰寫 `docs/onboarding/concepts.md`：spec/job/slice/work 名詞關係（沿用 UX 規格 §9）。
- [ ] 撰寫 `docs/onboarding/admin.md` 與 `docs/onboarding/runbook.md`：日常維運操作與事故 SOP。
- [ ] README 補「新手上手」導覽段，連結七份文件。

### 3. 同步與驗證

- [ ] 新增 `changelog.d/onboarding-docs.md` fragment，`CHANGELOG.md [Unreleased]` `### Added` 加入含 `onboarding-docs` 字樣的條目。
- [ ] `python3 -m pytest tests/ -q` 全綠（既有測試不受影響）；`python3 -m policy_check --repo .` 0 fail（含 R-18/R-21/R-22）；`git diff --check` 乾淨。
- [ ] 完成後勾選 `openspec/changes/onboarding-docs/tasks.md` 對應項並以 conventional commit 提交（不得改動本 plan 檔）。
