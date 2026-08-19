# gate-scope

- **`#721`：派工 prompt 的 gate **適用範圍**由 harvest 端同一支判準導出，`test_policy=none`
  的卡不再被告知「Manager 會重跑 pytest 並用它判你的 passed」。** 現場 job
  `wf-6c37c77ca1-worktree-isolation-8`：`worktree-isolation` 這張卡的 `test_policy` 逐字是
  `"none"`，harvest 端 `terminal_contract.expected_gate_names_for_test_policy()` 對它回
  `frozenset()`（docstring 甚至逐字點名這張卡），dispatch 端的 `allowed_names` 卻是 operator
  **全部** `PSC_GATE_CMD_*` 宣告，與 `effective_test_policy` **無關**；整段 prompt 只在
  `red-required` 分岔，`"none"` 沒有任何處置。於是模型讀到「after your process exits the
  Manager re-runs exactly these commands ("pytest" = `python3 -m pytest -q`), and a passed
  status is judged against those real results」就去跑 pytest，在 `#716` 選項 F 之後的
  `-s read-only` 沙箱下死於 `No usable temporary directory available`，terminal 回 `failed`，
  Manager 依 `_retryable_nonpassing_workflow_terminal` 自動重派 ⇒ **確定性無限迴圈**。
  `#540` 把 gate **名稱**從 prompt 端手寫改成機械產生；本票是同一個錯誤的另一半——名字機械
  化了，**適用範圍**沒有。
- **修法**：新增 `gate_ledger.card_requires_gate_evidence()`（轉呼叫
  `terminal_contract.expected_gate_names_for_test_policy`，**不在 prompt 端另寫第二份判準**）
  與 `gate_ledger.card_gate_names()`；`allowed_names`、`status_policy`、`gate_evidence`
  的說明三段都由它導出。契約不要求模型交出 gate 結果的卡（`test_policy` 為 `None`／`"none"`）
  拿到專屬文字：`allowed_names` 為空、逐字要求 `gate_evidence: []`、且不出現任何逼模型自己去跑
  gate 的句子（連泛用前言的 `every deterministic gate you ran (OpenSpec / pytest / policy)`
  都不發）。`focused`／`full`／`red-required` 的卡**逐字不變**，由
  `tests/test_gate_scope_test_policy_721.py` 內的原文複本常數釘住。
- **範圍收窄只做布林，不做名稱集合的 ∩**：`expected_gate_names_for_test_policy()` 回的是
  **測試**這一個訊號（`RED_REQUIRED_TEST_GATE_NAME`），不是「這張卡會被判哪些 gate」。拿它去
  ∩ ledger 的 gate 名稱集合，多宣告一個 `openspec`／`policy` 的 operator 會收到「Manager 只
  重跑 pytest」的 prompt，而 `authorize_terminal` 的矛盾偵測照樣拿 `openspec` 的失敗把
  `passed` 打掉——那是本票的**鏡像缺陷**（dispatch 講的範圍比 harvest 真正判的窄）。不變式是
  「dispatch 講的判定範圍 == harvest 真正判的範圍」，由
  `test_multi_gate_card_prompt_shows_every_gate_harvest_judges` 釘住。
- **`test_policy=none` 的卡仍據實揭露 Manager 自己那一次 gate 執行**：
  `gate_runner.ensure_gate_ledger()` 只看 phase（`build`）**不看 `test_policy`**，所以這種卡
  照樣會被 gate 執行身分重跑宣告的 gate 並寫進 ledger（實機 ledger
  `wf-6c37c77ca1-worktree-isolation-3.gates.json` 內確有一列 `pytest`），而
  `authorize_terminal` 對 ledger 裡**任何**非 passed 的 gate 都 fail closed。因此文案是
  「**不要你跑**，但 Manager 仍會自己跑並據以判定」，而不是「這張卡完全沒有 gate」——後者會把
  範圍講小。兩件事各有回歸測試釘住，未來若改成 none 卡不跑 gate，揭露句要一併收掉。
- **`#716` 選項 F 的隱性邊界寫進 `trust_root.SANDBOX_MODE_DERIVATION`**：
  `builder-write-forbidden` 那一列補上「`-s read-only` 之下**任何需要暫存檔的命令都會失敗**，
  `/tmp` 也不例外——『不寫工作區』不等於『不寫任何地方』」。
- 新增 `tests/test_gate_scope_test_policy_721.py`（17 個測試，修復前 12 紅）；
  `tests/test_retry_feedback_context_606.py` 的既有斷言改為明示 `test_policy="focused"`
  （prompt 文字逐字不變）。全套 pytest：4691 passed、36 skipped、65 subtests passed。
