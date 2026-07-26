---
status: accepted
work_item: driving-cortex-skill
---

# driving-cortex-skill Todo

## Tasks

- [ ] 將 issue #177、active OpenSpec change `2026-07-26-driving-cortex-skill` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex（gpt-5.3-codex-spark）完成 #177 driving-cortex skill（TDD）：`skills/driving-cortex/SKILL.md` 七段骨架 + `tests/test_driving_cortex_skill.py` 契約測試。
- [ ] ForeignReview（agy/gemini-3.6-flash-high）adversarial-review 通過；operator（Copilot CLI session）驗收核可。
- [ ] 一個沒跑過 cortex 的 agent 讀完 skill 能獨立完成 dogfood 批次（claim→build→review→attest→merge）；全文無個人絕對路徑／使用者名／雇主識別（R-21）；pytest/policy_check 全綠。