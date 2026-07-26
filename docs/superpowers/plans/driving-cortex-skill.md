---
status: accepted
work_item: driving-cortex-skill
---

# driving-cortex-skill Plan

## Tasks

### 1. TDD RED

- [ ] `tests/test_driving_cortex_skill.py`：
  - `test_skill_file_exists`：`skills/driving-cortex/SKILL.md` 存在。
  - `test_skill_has_description_frontmatter`：frontmatter 含 `description` 且含觸發詞（dogfood cortex / 派工 cortex / cortex work）。
  - `test_skill_covers_seven_sections`：七段關鍵標題皆出現（心智模型／開一個 dogfood 批次／驅動桿／執行器設定／每批 merge 後部署／生命週期特性／已知坑）。
  - `test_skill_no_personal_paths`：全文無個人絕對路徑、使用者名、雇主／廠商識別（R-21）。
  - `test_skill_no_unsafe_bypass_as_daily_guide`：`--dangerously-bypass-approvals-and-sandbox` 不作為日常指引（僅允許出現於明確標示的情境命令脈絡）。
  - 先確認 RED（SKILL.md 不存在 → 失敗）。

### 2. 撰寫 SKILL.md

- [ ] `skills/driving-cortex/SKILL.md`：frontmatter `description` 觸發詞；頂部兩視角分工（引用 B8 `docs/onboarding/*`）。
- [ ] 七段內容（issue #177 骨架）：心智模型、開一個 dogfood 批次、驅動桿、執行器設定、每批 merge 後部署、生命週期特性、已知坑。
- [ ] 恢復桿與已知坑以 checklist 呈現（可逐項操作）；關聯 issue 以 `#N` 標注。

### 3. 同步與驗證

- [ ] README 補 skill 導覽一行（R-18）。
- [ ] `changelog.d/driving-cortex-skill.md` fragment；`CHANGELOG.md [Unreleased]` `### Added` 加入含 `#177` 條目。
- [ ] `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail（含 R-18/R-21/R-22）；`git diff --check` 乾淨。
- [ ] 勾選 `openspec/changes/2026-07-26-driving-cortex-skill/tasks.md` 並以 conventional commit 提交（不得改動本 plan 檔）。