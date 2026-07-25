---
status: accepted
work_item: fix-git-runner-cwd
---

# fix-git-runner-cwd Todo

## Tasks

- [ ] 將 issue #99、active OpenSpec change `2026-07-25-fix-git-runner-cwd` 與本 Todo 綁定為同一 confirmed Work Item。
- [ ] coordinator 派工 codex（gpt-5.3-codex-spark）完成 #99 修復（TDD）：`_default_git_runner` 改 `git -C repo_root`、installer 模板加 `WorkingDirectory`。
- [ ] ForeignReview（claude/sonnet）通過；operator（Copilot CLI session）對抗 review 核可。
- [ ] pipx 重裝後從 systemd 啟動之 manager daemon 可成功 fanout（驗證 #99 根因修復）。