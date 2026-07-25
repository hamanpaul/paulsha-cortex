---
status: accepted
work_item: dispatch-reliability-batch
---

# Tasks

- [ ] 1.1 RED：`tests/test_dispatch_reliability.py` 涵蓋 #152 分級 timeout 與 pending 路徑、#100 DispatchReadyError 摘要與 tick response 透傳與 log 時間戳、#99 git `-C` 與 installer WorkingDirectory。
- [ ] 1.2 `coordinator/cli.py` 分級 timeout + pending 語意 + exit code 常數（#152）。
- [ ] 1.3 `coordinator/autonomy.py` `DispatchReadyError` per-slice 摘要（#100）。
- [ ] 1.4 `coordinator/manager_daemon.py` tick response `errors` 透傳 + log ISO-8601 前綴（#100）。
- [ ] 1.5 `coordinator/dispatcher.py` `git -C repo_root`（#99）；installer 模板 `WorkingDirectory`（#99）。
- [ ] 1.6 `changelog.d/dispatch-reliability.md` 與 `CHANGELOG.md [Unreleased]` `### Fixed` 同步（#152/#100/#99）；README 對應段同步（R-18）。
- [ ] 1.7 focused regression 全綠、`policy_check --repo .` 0 fail、`git diff --check` 乾淨。