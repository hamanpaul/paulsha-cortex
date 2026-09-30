### Fixed

- **#1234 builder contract 列出 work item 的 todo**：commit-required 卡除了 `tasks.md`，也要一併勾選 work item 的 plan-kind todo，並明講這些檔案只能改勾選狀態。以 untracked 形式 seed 的輸入不列入。修正前，#716 canary 的 builder 勾完 tasks.md 卻漏勾 workstream todo，Copilot review 回報 finding，ship 因此停住（#1234）。
