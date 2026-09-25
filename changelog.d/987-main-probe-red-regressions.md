# #987 main-probe RED regressions

- 新增 `tests/test_main_probe_gate_987.py` bare-origin RED regressions，固定兩個尚未實作的缺口：
  clean-behind Candidate 目前仍會誤進 preflight／push／建 PR，以及 `origin/main` fetch 不可用時
  `resume` 仍誤回 `delivery-in-progress` 而非 `main-sync-unavailable`。
