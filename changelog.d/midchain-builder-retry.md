---
type: fix
scope: coordinator
---
**Issue #545：`retry-build` 只受理最後一張 builder 卡，中段卡採信失敗後沒有契約內的重派路徑**

run `workflow-084f75e2178cf7547476`（#540 的殘留項）卡在 build 階段的中段卡
`tdd-red`：builder 交付的 RED commit 合格、ledger 已由 #540 新增的
`regenerate-gates` 重生成正確（`pytest: failed` = 合格 RED），但**舊 job 的
terminal envelope 是模型輸出**——自報 gate 名 `'focused pytest RED expectation'`，
envelope 屬契約內不可竄改的證據，`resume` 重新採信仍必敗於
`TerminalContractError: terminal 宣稱跑了 gate '...'，但 manager 的 ledger 沒有這
一項`（0815 實證）。#540／PR #541 已把 canonical gate 名機械注入 dispatch prompt
的 `allowed_names`，因此**新的** `tdd-red` job 會產出可採信的 envelope；缺的只是
「重派中段 builder 卡」這條路——`retry-build` 只受理最後一張 builder 卡、
`recover-pre-candidate` 要求 null candidate（`worktree-isolation` 早已錨定
candidate）、`abandon` 會連合格的 RED commit 與一個世代一起燒掉（該 run 已耗
2/3）。

### 修法：新增 `retry-card` work action

以 exact WorkflowRun CAS 加卡名定錨，原子清掉 `needs_human` facet 並讓 manager 以
**原卡片契約**重派一個新 job。舊 job 與舊 envelope 一個位元組都不動，原樣保留供
稽核；重派只允許產生新 job 與新 envelope。prompt 走既有的 `_workflow_job_prompt`
（含 #540 的 `allowed_names` 注入），沒有第二條組裝路徑；dispatch 沿用
`dispatch_workflow_card(force_new_build=True)`，dispatch 失敗時 `needs_human` 會被
補回去，不留下「facet 清了但沒派出去」的中間態。

fail closed 條件：exact WorkflowRun CAS、run 必須 ongoing、帶 `needs_human`、在
build phase、無 active job、`--card` 必須**正是**下一次 dispatch 會派的那一張
（build phase 內最早一張非 passed 的 builder 卡，判準直接取自
`manager._current_workflow_step`，兩邊不得漂移）、該卡不得已綁定
`workflow_evidence`（已採信的 evidence immutable，不得以重派名義覆寫）、且該卡必須
已有一顆終止的 job（從未派過的卡屬 `resume` 的職責）。任一條不成立即拒絕，不做
任何 side effect。註冊面比照 #540 的 `regenerate-gates`：`control/contract.py`、
`coordinator/cli.py`（`--card`）、`porcelain/recover.py`、umbrella `_WORK_HELP`
全數登錄。

### 取捨：issue 建議 (b)，不放寬 `retry-build`

`retry-build` 是 candidate 修復語意——`_manager_reset_workflow_for_retry_build` 會
把目標卡的 `step.action` **覆寫**成 repair 文案，中段卡走那條路等於把卡片自己的
指示（「新增可重現缺口的 RED regression test」）抹掉，重派出去的 builder 會被叫去
修 candidate 而不是重寫 RED；它的 admission 另外還綁「unbound terminal builder
evidence（exited/0）」與「只有最後一張卡 pending」兩個與中段卡無關的條件。放寬等
於在同一個 action 內開一條語意分岔並動到既有呼叫端；獨立 action 讓「重派原卡」與
「派人修 candidate」是兩個各自 fail-closed 的入口（與 #260 對
`recover-repair-commit` 的取捨同型）。`retry-build` 的 CAS 與 admission 一字未改，
另補回歸樁鎖定它仍拒絕中段卡。

### 順帶（#546 部分）：needs_human 的 `next_actions` 曝光面

`claim._resume_decision` 只看得到 run 的 phase 與 planning failure 記錄、拿不到
job 層事實，因此 build 卡卡住時它宣告的唯一出口是 `abandon`（＝燒掉一個世代）。
work action 層（拿得到 `JobRegistry`）新增 `_build_phase_recovery_actions`，以與
`regenerate-gates`／`retry-card` **完全相同**的前置驗判定兩者是否真的會被受理，是
才補進 `resume` 回傳的 `next_actions`——只宣告會成功的動作，拿不準就不宣告（#382
的教訓）。`claim.py` 的純決策邏輯未動。

新增 `tests/test_midchain_builder_retry_545.py`（23 個回歸測試：中段卡重派產生新
job、`retry-build` 最後一張卡語意不回歸、run/card CAS mismatch 拒絕、已採信卡拒絕
重派、facet 原子性、CLI／contract 登錄面）。
