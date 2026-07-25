---
status: accepted
work_item: fix-git-runner-cwd
---

# Tasks

- [ ] 1.1 RED：`tests/test_fix_git_runner_cwd.py` 斷言 `_default_git_runner` argv 含 `["git","-C",<repo_root>]`、失敗訊息含 `-C`、installer render 含 `WorkingDirectory=`（manager+monitor）。
- [ ] 1.2 `coordinator/dispatcher.py` `_default_git_runner` 改 `git -C paths.repo_root()`（#99）。
- [ ] 1.3 installer 模板 `cortex-manager.service` 與 `cortex-monitor.service` 加 `WorkingDirectory=<repo_root>`（#99）。
- [ ] 1.4 `changelog.d/fix-git-runner-cwd.md` 與 `CHANGELOG.md [Unreleased]` `### Fixed` 同步（#99）；README 對應段同步（R-18）。
- [ ] 1.5 focused regression 全綠、`policy_check --repo .` 0 fail、`git diff --check` 乾淨。