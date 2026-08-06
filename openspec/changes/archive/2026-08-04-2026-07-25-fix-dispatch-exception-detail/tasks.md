---
status: accepted
work_item: fix-dispatch-exception-detail
---

# Tasks

- [x] 1.1 RED：`tests/test_fix_dispatch_exception_detail.py` 涵蓋 DispatchReadyError 摘要、tick response `errors` 透傳、log ISO-8601 前綴。
- [x] 1.2 `coordinator/autonomy.py` `DispatchReadyError.__str__` per-slice 摘要（#100）。
- [x] 1.3 `coordinator/manager_daemon.py` tick response `errors` 透傳 + log ISO-8601 前綴（#100）。
- [x] 1.4 `changelog.d/fix-dispatch-exception-detail.md` 與 `CHANGELOG.md [Unreleased]` `### Fixed` 同步（#100）；README 對應段同步（R-18）。
- [x] 1.5 focused regression 全綠、`policy_check --repo .` 0 fail、`git diff --check` 乾淨。
