---
type: fix
scope: coordinator
---
**Issue #731 (A)：候選 git base 補上可稽核的重新凍結入口 `cortex work refreeze-base`**

長壽 work item 的候選基底永遠停在第一次 claim 的那個 commit，`abandon` ＋
`reset-reclaim-budget` ＋ `work start` 換不掉。0819 深夜實測序列逐字：`work start`
對還有 active workflow 的 work item 回 `action=resume / reason=active-workflow`，
不走新 claim ⇒ 基底原封不動；mirror 已是 `7eb707b`，候選樹仍是 `59a7a9b`。後果是
已在 main 的修法**結構上永遠到不了**正在跑的那個 run——manager 的獨立 gate 重跑的是
候選自己那棵樹的測試套件，於是每一次重派都撞同一條紅，`retry-card`／`recover` 一律
無效。

**查證到的權威來源（非推測）**：候選基底只有一處被消費——
`manager._dispatch_workflow_card` 建**首張 build 卡**工作區時讀的
`WorkflowRun.frozen_readiness["base_sha"]`，一路傳進
`seams.ScriptWorktreeCreator.create(..., base_sha=…)`。該欄位為 `None` 時
`create()` 退回 `self._base`，而 dispatch 建 creator 時傳的是字面 `base="main"`
⇒ 實際基底是**來源樹的本地 `refs/heads/main`**。而 `readiness_checker` 在
production 從未被接線（`execute_work_action` 的預設值是 `None`，manager daemon
也沒有傳），因此實機 run 的 `frozen_readiness` 恆為 `None`，基底就是那條沒有人
推進過的本地 `main`——`git fetch` 只動 `refs/remotes/origin/main`，這正是「mirror
已更新、候選樹沒動」的機制。新動作把新基底寫進 dispatch 真的會讀的那一格，不新造
第二份真實來源，並順帶把 run 從「隱式跟著本地 main 漂」升級成「明示 pin 在一個
SHA」——hermetic pinning 只有更強，沒有放寬。

新增 `cortex work refreeze-base`（形狀比照 `reset-reclaim-budget`）：

- **CAS ＋ bounded 稽核欄位**：`--expected-run-id`（exact WorkflowRun CAS）＋
  `--actor` ＋單行 `--reason`，界限與 `abandon`／`retire-delivered`／
  `reset-reclaim-budget` 逐字相同，由 `control/contract.py` 在所有入口的收斂點強制。
- **mirror fetch 走 claim 用的同一支 probe**（`claim_readiness.base_sha_probe`），
  不另寫一次 fetch。
- **入場條件 fail-closed（寧可拒絕也不做半套）**：唯一 `ongoing` canonical run ＋
  run id 精確吻合；phase ∈ `claim`／`define`／`plan`／`build`；`candidate_head`／
  `verified_head` 皆為 `None`（已有被採信 candidate 時 base 改由
  `_workflow_build_handoff_base()` 決定，重新凍結會是**靜默 no-op**）；無
  `dispatched`／`running` job；無已發佈交付物；且新基底必須是**每一條已記錄基準**
  （目前凍結值／未凍結時的本地 `main`、本 run 每個 job 的 `dispatch_head`、build
  branch 現位置）的後代或相等——非 fast-forward 一律拒絕。
- **#613 前置檢查**：build branch 上若還有新基底以外的 commit，下一拍 provision 必定
  撞 `existing worktree branch has commits outside requested base`。判準與
  `ScriptWorktreeCreator.create()` 的守衛是**同一個** `git merge-base --is-ancestor`
  述詞，且在改任何狀態**之前**就問，因此不會出現「refreeze 成功、下一拍才炸」。
  branch 名的推導抬成 `manager.workflow_build_branch()` 單一導出點，dispatch 與
  refreeze 問到的是同一條 branch。
- **evidence**：immutable `cortex-work-candidate-base-refreeze/v1`（舊／新基底 ＋ 舊
  基底的來源、mirror 的 `remote_fetch` 結果、全部 fast-forward 基準、build branch 與
  其位置、重新凍結前的 phase／facets、actor／reason／`created_at`），落在
  `<coordinator_root>/evidence/work-candidate-base-refreeze/{run_id}-{hash}.json`，
  原子寫入與命名慣例比照 `cortex-work-abandon/v1`；evidence ref append 回 run。已凍結
  在同值時回報 `already_current`，不寫第二筆。
- **frozen 集的處置**：run 已有凍結集時**逐欄保留**、只換 `base_sha`（其餘欄位是當初
  那次 readiness transaction 的產物，這次沒重跑，不得順手覆寫）；沒有凍結集時寫入
  `cortex-candidate-base-freeze/v1` 的最小凍結記錄——刻意**不**假裝成
  `pre-claim-readiness-frozen-set/v1`，那個 schema 的語意是「六道 readiness 關卡都通
  過了」。
- **出口狀態 == 入口狀態**（#728／PR #729 同一紀律）：不動 `current_phase`、`facets`、
  `candidate_head`、任何 step 的 `gate_result`；唯一狀態變更是
  `frozen_readiness["base_sha"]` 與 append 一筆 evidence ref ⇒ 重新凍結後的 run 狀態
  就是重新凍結**之前**那個狀態，結構上不可能不是後續每一拍的合法入口狀態。推進仍由
  既有出口負責（`needs_human` 時回傳附 `next_actions`）。

迴歸釘住：`tests/test_candidate_base_refreeze_731.py` 走**正式** dispatch 路徑（真
`ScriptWorktreeCreator`、真 git repo、真 `retry-card` work action、
`force_new_card=True` 就是 `manager_daemon` 傳的那一個），驗重新凍結後新派工的
worktree `git rev-parse HEAD` **逐字等於**新的 `origin/main`；另釘住「再前進一次而
未重新凍結時候選樹不得跟著漂」，證明 hermetic pinning 沒有被放寬。
