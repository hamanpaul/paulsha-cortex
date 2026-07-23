---
status: accepted
work_item: porcelain-bootstrap
---

# porcelain-bootstrap Todo

## Tasks

- [x] 將 issue #91、active OpenSpec change `porcelain-bootstrap` 與本 Todo 綁定為同一 confirmed Work Item。
- [x] coordinator 派工 copilot（gpt-5.4）完成 `bootstrap` 命令（preflight + service + inspect 整合，TDD）；若 B4/B3 尚未落地，先以 fake 模組驗證介面契約。
- [x] ForeignReview（claude/sonnet）、自動 push/PR、bot review、merge 與 archive 閉合。
- [x] 在乾淨環境（或等效乾淨 fixture）下，從執行 `cortex bootstrap` 到完成第一個 quickstart 流程可在 10 分鐘內完成；`--dry-run` 準確預覽而不動作。
