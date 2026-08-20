# 734-argv-semantic-assertions

- **`#734` wrapper 斷言由整串 argv substring 改為逐 token 語意判定**——
  `test_planning_wrapper_has_no_gate_bundle_verdict_sentinel` 原以
  `banned not in " ".join(command)` 驗「wrapper 不自產 shell／gate／bundle／verdict／
  sentinel」，會被任何恰好含這些字的**路徑值**誤判：gate 執行帳號叫 `cortex-gate`，
  pytest 用帳號名組 tmp 根目錄，`#730` 又把 `-o` 落點正確地搬進 `tmp_path`，兩者相乘
  使該條**只在 gate 環境紅**（本機與 CI 全綠），gate 重跑 pytest 直接
  `GateContradictionError` 擋死演示鏈。改法不是排除 `tmp_path`（下一個含 `gate` 的
  合法路徑照樣誤判），而是把判準換成語意：token basename 不得是 shell／git、旗標**名**
  （`=` 前、去前導 `-`）不得含 gate／bundle／verdict／sentinel／exit、裸字 token 不得
  是 wrapper 詞彙、任何 token 不得以 `.exit` 結尾；`--skip-git-repo-check` 這類合法
  旗標與路徑值都不再進比對範圍。四個 mutation（`--gate-bundle` 旗標、`bash -c` 包裹、
  `.exit` sentinel、`git bundle` 呼叫）驗證斷言仍會紅。是 `#723` 記的「測試隱含
  operator 環境」一類的第二個實例（第一個是 umask，`#724`）；全 tests/ 掃過一輪，
  其餘 joined-argv 斷言都釘在特定片語或生成文字上，無同型風險。
