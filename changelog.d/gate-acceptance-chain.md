---
type: fix
scope: coordinator
---
**Issue #540：tdd-red terminal 採信三段連鎖——builder 的正確 RED commit 無法被採信**

現場 run `workflow-084f75e2178cf7547476`：define 一次走完、`worktree-isolation`
通過、builder 交付了合格的 RED commit（`1e4f04b`），terminal 採信卻連續撞上三個
獨立缺陷，run 停在 needs_human。三段各自修復如下。

### 段 1：gate 宣告缺漏在開工前就被擋（`cortex doctor`）

manager env 漏 `PSC_GATE_CMD_PYTEST` 時，job 結束由 wrapper 寫出的 ledger 是
`gates: []`，帶 `test_policy` 的 build 卡必然以 `gate-ledger-missing-expected-gate`
fail closed——這是正確的反自證行為，但事前完全沒有診斷，錯誤只進 `manager.log`
（#527 diagnostics 家族），operator 只能在 builder 已經跑完並交付合格 candidate
之後才發現。

新增 `doctor._gate_declaration_probe`（probe 名 `gate-declarations`）：以 packaged
deck 每張卡的 `execution.test_policy` 經
`terminal_contract.expected_gate_names_for_test_policy`（**與 harvest 端同一個
判準**，非另立一套）導出應驗 gate 集合，比對 effective service env 的
`PSC_GATE_CMD_*` 宣告。未涵蓋應驗 gate、或宣告本身不合法（typed argv 違規／shell
wrapper）皆為 required fail，訊息直接給出缺哪個 gate 與範例宣告；deck 資料讀不到
時降為 warn，不冒充 gate 宣告失敗。

為此把 `manager._expected_gate_names_for_test_policy` 的實作移到
`terminal_contract.expected_gate_names_for_test_policy`，manager 端保留同名薄轉呼
叫（既有呼叫端與測試不變），讓 doctor 不必為共用判準 import 整個 manager。

### 段 2：`regenerate-gates`——ledger 凍結後的官方重驗路徑

ledger 是 job 結束當下依當時 env 生成的檔案，之後即凍結。env 修好之後，契約內沒有
任何路徑能讓它重新產生：`resume` 只重讀同一份舊 ledger 再拒一次；`retry-build`
要求「只剩最後一張 builder 卡 pending」，tdd-red 是中段卡，拒；
`recover-pre-candidate` 要求 null candidate，`worktree-isolation` 早已錨定
candidate，拒。唯一出路是 operator 手動跑 `python -m ...gate_ledger` CLI。

新增 work action `regenerate-gates`（`cortex work regenerate-gates <work_id>
--repo owner/repo --expected-run-id <workflow-...>`）：以 exact WorkflowRun CAS
定錨，對該 run 最新一個 gate-ledger 相關 phase、已終止、log 與 worktree 都還在的
job，依**當前**宣告重跑 gate 並原子覆寫 ledger。

它刻意只做這件事——**不改判**：不重派任何模型、不動 builder 的 commit、不改任何
run/slice 狀態；run 仍停在 `needs_human`，採信與否由既有的 `resume` → harvest
流程重新評估（回傳值以 `next_actions: ["resume"]` 明示）。fail closed 條件：run
必須 ongoing 且帶 `needs_human`、CAS 精確相符、必須真的找得到符合條件的 job log
與 worktree、gate 宣告必須合法；任一不成立即拒絕且不產生任何 side effect。動作在
`control/contract.py`（含 `expected_run_id` 驗參）、`coordinator/cli.py`、
`porcelain/recover.py` 與 umbrella `_WORK_HELP` 一併登錄。

### 段 3（主修）：dispatch prompt 機械生成 canonical gate 名稱

`terminal_contract.authorize_terminal` 要求 envelope 自報的 `gate_evidence[].name`
⊆ ledger 實際跑過的 gate 名稱（由 `PSC_GATE_CMD_<NAME>` 導出，`PSC_GATE_CMD_PYTEST`
→ `pytest`），但派工 prompt 從來沒有把那個集合告訴模型，`terminal_schema` 只寫
`{"name": "gate name"}`。實測 builder 自報 `'focused pytest RED expectation'`，
採信因 `gate-evidence-unknown-gate` 必敗——與 `#486`（foreign-review prompt 缺
validator enum）同構：**要求模型精確命中一個它看不到的集合**。

修法比照 `#521`（必要標題由判準常數機械產生）：

- `gate_ledger.declared_gate_names()` 由 `load_gate_specs()`（產生 ledger
  `gates[].name` 的唯一處）導出 canonical 名稱集合；`ledger_gate_names()` 是它的
  不拋版本（宣告不合法時回 `GATE_SPEC_FAILURE_NAME`，與 CLI 寫出的 ledger 一致），
  供 prompt 使用而不因 operator 設定錯誤炸掉派工；
- `gate_ledger.gate_evidence_name_hint()` 由上述集合機械產生 prompt 文字；
- `manager._workflow_job_prompt` 新增 `env` 參數（預設 `os.environ`，與 launcher
  交給 gate ledger writer 的 env 同源），把 `allowed_names` 與說明文字注入
  `terminal_schema.gate_evidence`。prompt 端不再持有第二份真實來源，宣告改動自動
  同步。

同時補上 `#307` 反轉語意的 prompt 面：`test_policy=red-required` 的卡另附
`red_required_policy`（由 `terminal_contract.red_required_status_hint()` 依
`RED_REQUIRED_TEST_GATE_NAME` 與 `PYTEST_EXIT_TESTS_FAILED` 產生），明示「ledger
顯示 pytest failed 時仍回 `status=passed` 並誠實自報 `pytest: failed`，反轉由
Manager 執行」——否則泛用 `status_policy`（「任一 gate failed 就回 failed」）字面上
與 tdd-red 卡的實際採信規則相反。一般卡完全不受影響。

### 測試

新增 `tests/test_gate_acceptance_chain_540.py`（15 個回歸測試，逐段對應上述三項，
含「prompt 的 enum 就是採信判準會接受的集合」端到端斷言：照 prompt 寫過、寫
`'focused pytest RED expectation'` 拒），並在 `tests/test_doctor.py` 補兩個
`run_doctor` 整合測試與 fixture 的 gate 宣告。判準值與既有採信行為零變更。
