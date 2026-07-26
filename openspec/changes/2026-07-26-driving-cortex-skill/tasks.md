---
status: accepted
work_item: driving-cortex-skill
---

# Tasks

- [ ] 1.1 RED：`tests/test_driving_cortex_skill.py` 鎖定 SKILL.md 存在、frontmatter `description` 觸發詞、七段標題、R-21 無個人路徑、unsafe bypass flag 不作日常指引。
- [ ] 1.2 `skills/driving-cortex/SKILL.md`：frontmatter `description` + 兩視角分工 + 七段骨架（心智模型／開一個 dogfood 批次／驅動桿／執行器設定／每批 merge 後部署／生命週期特性／已知坑）；恢復桿與坑以 checklist 呈現。
- [ ] 1.3 README 補 skill 導覽一行（R-18）；`changelog.d/driving-cortex-skill.md` 與 `CHANGELOG.md [Unreleased] ### Added`（#177）。
- [ ] 1.4 `python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail；`git diff --check` 乾淨。