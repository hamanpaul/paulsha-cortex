# #987 main-probe regressions

- 新增 `tests/test_main_probe_gate_987.py` bare-origin regressions，覆蓋已落地的
  main-probe gate 行為：clean-behind Candidate 會在 preflight／push／建 PR 前
  fail-closed、`origin/main` fetch 不可用時 `resume` 會回 `main-sync-unavailable`
  並在修復後重新 probe，且 full candidate SHA 仍以大小寫不敏感比對維持 exact
  object-id gate。
