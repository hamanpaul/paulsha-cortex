# link-delivered-work-items-20260915

- **`.cortex/work-items.yaml` 補登錄 33 個已交付／待派工 work item 的 `github_issue`
  link，清掉 `cortex status` 的 `not_claimable` 反覆觀測**（`#669` 表象、`#895` 治標）——
  31 個管線外交付、只剩規劃文件殘留的 work item 綁定原交付 issue（皆 CLOSED，
  `decide_auto_claim` 走 `ignore: auto-label-missing`，不建 run、不派工）；其中 6 個沒有
  專屬 issue 者以 closeout 票 `#898`–`#903` 作結案紀錄（v1 被 -v2 重識別的兩件、
  Phase 2 隨 PR #785／#794／#798 交付的四件）。`agy-print-timeout-only` 綁 `#824`、
  `r3-testpilot-case-corpus` 綁第二輪承接票 `#904`，兩者將以 `cortex:auto-on-going`
  交由管線派工。另還原三筆 09-14 在本機 checkout 以 `work link` 寫入但未提交的登錄
  （`#879`／`#880`／`#828`）與兩個 openspec link。
