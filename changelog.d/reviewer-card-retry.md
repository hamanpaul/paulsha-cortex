# Issue #569：reviewer 卡的 `retry-verify` 只重置不重派，形成 #545 同型 catch-22

## 現場（0815，run `workflow-084f75e2178cf7547476` verify 階段）

1. 16:48 verification job `wf-865ecb7f70-verification-484`（agy，#568 的權限剖面
   缺陷）exit 0，但 log 一行 JSON envelope 都沒有 → harvest 撞
   `workflow terminal log has no JSON evidence` → `needs_human`。
2. 17:3x operator 下 `retry-verify`，回應 `verification-rerun-dispatched` 但
   **`job: None`**——它只重置卡片與 facet，沒有在同一個 action 內派新 job，也沒有
   supersede 舊 job。
3. 17:3x–21:24 約 20 個 tick 期間沒有任何新 job 被派：run 是 ongoing、facets 空、
   verification pending、無 active job，而 dispatch 看到「這張卡已經有 job」就把舊
   的那顆原樣回傳，resume 因此每次重讀同一份壞 log。run 對 tick 實測隱形四小時。
4. 21:24 `needs_human` 原地回鍋。淨效果＝四小時＋回到原點。

根因與 #545 同型：卡片最新的終止 job 輸出損壞時，harvest 永遠贏過 dispatch。
builder 卡已由 PR #552 的 `retry-card` 解決，reviewer 卡當時沒有等價物——
`retry-card` 明文 `requires a builder card`，而 `retry-verify` 是 slice-lane 時代
的 **phase 級**重置，只做半套。

## 修法：`retry-card` 放寬到 reviewer persona 卡（issue 建議 (a)）

`registry.RETRY_CARD_PHASE_PERSONA`（`build→builder`、`verify`／`review→reviewer`）
成為 work action 層與 registry 層共用的單一判準。`retry-card` 因此同時受理
build phase 的中段 builder 卡（#545 原有語意，一字未改）與 verify／review phase
的 reviewer 卡（`verification`／`code-review`／`adversarial-review`）：

```
cortex work retry-card <work_id> --repo owner/repo \
  --expected-run-id workflow-... --card verification --actor operator
```

#552 的硬約束逐條沿用：

- **exact WorkflowRun CAS ＋卡名定錨**：`--card` 必須正是
  `manager._current_workflow_step` 下一次會派的那一張，且 persona 與 phase 相符。
- **evidence immutable**：已綁定 `workflow_evidence` 的卡拒絕重派。verify／review
  的判斷以**現在這個 candidate** 為錨（與 `_dispatch_workflow_card` 的 `matching`
  同一組判準）——上一代 candidate 的歷史 evidence 不參與判斷，否則
  「retry-build 換過 candidate 之後 reviewer 卡再次卡住」會變成無解，等於再造一次
  同一個 catch-22。
- **舊 job 一個位元組都不動**：不改 status、不改 evidence，原樣保留供稽核；重派只
  產生新 job 與新 envelope。（這是與 `retry-verify`／`retry-review` 的關鍵差別——
  那兩者會把舊 exited job 改標 `failed`。）
- **身分重新解析**：reset 只清該卡的 `executor`／`model`／`domain`，新 job 的身分由
  identity registry 在 dispatch 當下解析，**不複製舊 job 的 executor／model**——
  #568 的 reviewer fail-over 正依賴這一點。卡片契約（`action`／`inputs`／`outputs`
  ／`test_policy`）全數保留，prompt 走既有的 `_workflow_job_prompt`。
- **facet 原子性**：`manager_daemon` 的強制重派分支（`retry-build`／`retry-card`
  共用）在 dispatch 拋例外或回 `None` 時把 `needs_human` 補回去，並依 #527 的診斷
  invariant 落一份結構化理由（`forced-card-retry-failed`）。**#569 的實測就是這個
  中間態撐了四小時**，因此另有一條總樁釘死：無論 dispatch 以哪一種方式失敗，run
  都不得停在「ongoing／無 needs_human／無 active job」。

配套的 dispatch 面改動：

- `manager._dispatch_workflow_card` 的 `force_new_build` 一般化為
  `force_new_card`（受理的卡同樣取自 `RETRY_CARD_PHASE_PERSONA`）。
- reviewer 卡的強制重派在派新 job 前**原子回收被取代 job 的 sandbox**：sandbox 目
  錄名是 `sha256(run_id:card:candidate)`，重派同一張卡＋同一個 candidate 必然撞上
  `stale reviewer sandbox requires reconciliation`。回收走既有的
  `_discard_reviewer_sandbox(require_candidate_unchanged=True)`，reviewer 動過
  candidate 時 fail closed——重派不得成為蓋掉這個事實的名義。只掛在 forced 路徑
  上，其餘既有路徑的 sandbox 回收點一個字節都不動。
- 分類沿用既有詞彙：verify 走 `verification-rerun`（`orchestrator_retry`）、review
  走 `review-handoff-failure`，candidate 完全沒變的重跑不計入 model failure 指標。

## 一併修：resume／status 的曝光面（#546 的一部分）

`_build_phase_recovery_actions` 改名為 `_phase_recovery_actions` 並涵蓋
build／verify／review——#569 的 operator 之所以改用 `retry-verify`，正是因為
`resume` 的 `next_actions` 對卡住的 verification 卡只說得出 `abandon`。判準仍與各
動作自身**完全相同**（只宣告會被受理的動作）。#527 引入的 `cortex status`
attention 條目共用同一個 helper，因此一併看得到 reviewer 卡的重派出口。

## 取捨（issue 的建議 (a) vs (b)）

**選 (a) 放寬 `retry-card`，不補完 `retry-verify`。**

1. `docs/superpowers/specs/fix-repair-commit-recovery-spec.md` R4 明文鎖定
   「`retry-verify`／`retry-review` 的 CAS 與 admission 不變」。補完 (b) 必須動它
   的 admission（它不吃 `expected_run_id`、也沒有卡名定錨，只有 candidate CAS），
   等於在被 spec 鎖定的既有動作上開語意分岔。
2. 兩者語意本來就不同層：`retry-verify` 是 **phase 級**重置（整個 phase 打回
   pending、清 gate_refs、把舊 exited job 改標 `failed`），`retry-card` 是**單卡**
   重派（只動指名的那一張，舊 job 一個位元組都不動）。把「不動舊 job」塞進
   `retry-verify` 會與它 #315 的既有職責（讓 dispatch 不要先 terminalize 舊 job）
   直接衝突。
3. 單一重派入口＝單一測試矩陣：builder 與 reviewer 卡共用同一組 CAS／immutable／
   原子性判準與同一條 daemon 補償路徑，未來只有一處會漂移。

代價：`force_new_build` 更名為 `force_new_card`（呼叫端只有 `manager_daemon` 與四
處測試引用），`_build_phase_recovery_actions` 更名為 `_phase_recovery_actions`
（呼叫端為 `_claim_action` 與 #570 新增的 `workflow_status_entry`）。`retry-verify`
／`retry-review`／`retry-build` 的 CAS 與 admission 一字未改，並補上回歸樁。

## 測試

新增 `tests/test_reviewer_card_retry_569.py`（23 項）：reviewer 卡重派產生新 job
且身分重新解析（舊 job 記的 agy/gemini 不在 registry，新 job 必須解析成
claude/anthropic）、卡片契約與下游卡片不變、舊 job 逐欄位不動、review phase 卡同
樣可重派、CAS／卡名／needs_human／終止 job／active job mismatch 全數 fail closed
且無 side effect、已採信拒絕、上一代 candidate 的 evidence 不誤判、registry 層競
態重驗、sandbox 撞名回收與 candidate drift fail closed、daemon 補償（含結構化理
由）與 facet 原子性總樁、`retry-verify` 既有呼叫端不破、`resume` 與 `cortex
status` 的曝光面。`tests/test_midchain_builder_retry_545.py` 的 builder 行為全數
維持綠燈（僅同步更名與一項 phase admission 樁改為釘「重派必須落在當前 phase 的當
前卡」）。
