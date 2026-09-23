---
status: accepted
work_item: review-gate-adjudication-exit
---

# Review gate `blocking-findings` 的 operator 裁決出口設計

## Decisions

### D1 `retry-review` 成為裁決通道的第三個寫入端，schema 與讀取端都不動

#752、#755、#814、#757 已經建好 operator 裁決通道：寫入端是 `retry-card`／`retry-build` 的 `--reason`，落成 `cortex-operator-adjudication/v1` evidence（content-addressed、唯讀、Manager-owned）；讀取端 `_operator_adjudications` 以 run 為單位取最近 ≤3 筆；dispatch 在每一張卡的 prompt 注入 `operator_adjudications`，並依 persona 附上 builder 或 reviewer 的 directive。#956 缺的只是 `retry-review` 這個寫入端。本票的實作方式：

- `_retry_review_action` 簽名加 `state_path`／`now_epoch`（keyword 參數，預設 `None`），allowlist 加 `reason`。allowlist 檢查之後立刻呼叫 `_validate_operator_adjudication_args(..., action="retry-review")`。
- 其餘前置驗證與 `_manager_reset_workflow_for_retry_review` 呼叫一字不改。
- reset 成功、`_recompute_and_persist_sizing` 之後，呼叫 `_record_operator_adjudication(run=<reset 前的 run>, card=<D2>, args=args, state_path=state_path, now_epoch=now_epoch)`。回傳 dict 補上 `adjudication_evidence` 與 `adjudication` 兩個鍵，形狀比照 `retry-card`。
- `execute_work_action` 的 retry-review 分支改傳 `state_path=resolved_state_path, now_epoch=now_epoch`。

evidence 的 schema、`_operator_adjudications`、`_workflow_retry_context`、`_workflow_job_prompt` 的 contract 鍵與 dispatch 接線都不改。

讀取端 `_operator_adjudications` 會把每筆 reason 截成 `RETRY_CONTEXT_EVIDENCE_LIMIT`（2000）字（manager.py:10022），本票保留這個行為。evidence 保存 ≤4000 字全文供稽核，prompt 只帶前 2000 字，retry-card／retry-build 現行也是如此。調高上限會同時放大三個寫入端的 dispatch prompt，不在本票範圍。spec R3、R8 與測試都以「前 2000 字逐字」為準，不要求 2000 字以後進 prompt。

### D2 evidence 的 `card` 取 reset 前的 rejected 卡

在函式內 lazy import `from .manager import _current_workflow_step`（與 `_retry_card_action` 同一種寫法），在 reset **之前**取 `target = _current_workflow_step(run)`：

```python
card = (
    target.card
    if target is not None
    else next((s.card for s in reversed(run.steps) if s.phase == "review"), "review")
)
```

blocking-findings 的 run 中，這張卡就是 `gate_result == "needs_human"` 的 review 卡（`code-review` 或 `adversarial-review`）。card 名只用於稽核與收據，注入範圍仍是 run 級：同一個 run 之後所有卡都讀得到這筆裁決，#757 語意不變。

### D3 寫入順序比照 `retry-card`：先驗證，reset 之後才寫

- reason 驗證放在最前面：裁決寫不進去，就不該動 run。
- evidence 在 reset 成功之後才寫。如果先寫，一次失敗的 reset（CAS mismatch、缺 frozen plan、有 active job）會留下孤兒裁決，而裁決是 run 級注入，孤兒會汙染同一個 run 後續的 dispatch。
- 已知窗口：reset 成功後 evidence 寫入拋例外，例外會上拋，run 已經重開 review，重派的 reviewer 讀不到裁決。這與 `retry-card`／`retry-build` 現行的窗口相同，列為 accepted risk。reviewer 大概率再判 rejected，operator 可以再帶 reason 執行一次 `retry-review`。
- daemon 在同一個 request 內依序完成 reset 與寫入，tick 不會插在兩者之間 dispatch。

### D4 review gate 判準不動；「接受」由 reviewer 在裁決下重審

Manager 不直接把 rejected 改判為 passed，原因有三：

1. foreign-review evidence 綁定 `reviewer_job_id`／`launch_identity`／candidate（manager.py:12640-12665），ship 段也讀它的 path/hash。Manager 自行產生一份 passed evaluation 等於偽造 reviewer 產物，違反 #540／#628 的作者歸屬。
2. #956 建議 2（依 severity 或 `recommendation` 文字改判）要解析自由文字，屬於不確定的判準。
3. #956 建議 3（`review-attest` 對 review 卡 gate 生效）會把 ship 段的 delivery 授權（取代 Copilot）和 foreign-review gate 混為一談，還得同時改 `work_bridge.py` 與 ship validator。

因此本票的「接受」出口是：`retry-review --reason` 讓 reviewer 在裁決下重審。「駁回」出口是既有的 `retry-build --reason`：builder 帶著裁決與 #750 的跨卡回饋修 candidate。兩條路都走既有的 gate 與既有的作者歸屬。

### D5 reviewer directive 的附加句從常數導出

在 `OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE` 的既有字串之後，串接下面這一句（英文，與既有 prompt 語言一致）：

```python
" When a ruling explicitly accepts or waives a specific finding or deviation, do not report "
"that finding again under a blocking category ("
+ ", ".join(sorted(foreign_review.BLOCKING_FINDING_CATEGORIES))
+ "); if you still record it, use a non-blocking category ("
+ ", ".join(sorted(foreign_review.VALID_FINDING_CATEGORIES - foreign_review.BLOCKING_FINDING_CATEGORIES))
+ ") and cite the ruling in its recommendation."
```

`manager.py` 在模組層已經有 `from . import review as foreign_review`（第 42 行），常數在 import 之後定義，因此可以直接引用。類別清單由常數導出，以後新增類別時句子不會與 gate 漂移。`OPERATOR_ADJUDICATION_DIRECTIVE`（builder 側）與 `_OPERATOR_ADJUDICATION_PREAMBLE` 都不動。

### D6 曝光面沿用各動作自身的前置條件（#382）

在 `_phase_recovery_actions` 做四件事：

1. 把 `reason_code` 的計算提到函式開頭，後面的 `copilot-*` 分支沿用同一個值。
2. 既有的 `list_jobs()` try/except（work_actions.py:2560-2563）多設一個 `jobs_readable` 旗標：成功時為 `True`，進 except 時為 `False`。except 分支仍把 `jobs` 設為 `[]`，讓既有的 regenerate-gates／retry-card 判定維持原樣。這兩者都要求正向 job 證據，讀取失敗時本來就不會宣告。
3. 在既有的「`run.current_phase in RETRY_CARD_PHASE_PERSONA` 且 run 沒有 active job」分支內，retry-card 判定之後，只在 `jobs_readable` 為真時呼叫新 helper `_blocking_findings_recovery_actions(run)`，回傳 `("retry-review", "retry-build")` 的子集。retry-review／retry-build 的前置條件沒有任何正向 job 證據，把讀取失敗當成「沒有 active job」，就會宣告一個可能被 registry reset 以 `refuses active workflow job` 拒絕的動作，違反 #382。
4. helper 的判準逐條對照 spec R5：
   - 共同條件：phase 為 review、status 為 ongoing、facets 含 `needs_human`、`reason_code == "blocking-findings"`。
   - `retry-review`：`isinstance(run.candidate_head, str) and run.candidate_head == run.verified_head`，`any(item.kind == "plan" for item in run.planning_authority)`，verify steps 非空且全數 passed。
   - `retry-build`：`isinstance(run.candidate_head, str)`，build steps 非空且全數 passed，passed 的 ship steps 至多一張且為 Manager-owned `openspec-archive`。

宣告的原則是只列一定會被受理的動作，拿不準的就不列。work action 層拿不到 `WorkAuthority`，因此不檢查「唯一 active canonical run」與 issue 授權，這與既有的 retry-card 宣告一致。

### D7 `next_step_hint` 在讀取時計算，不持久化

新增純函式 `blocking_findings_next_step_hint(*, work_id, repo, candidate)`，格式檢查沿用 `claim.needs_human_next_step_hint` 的 `hint_value` 寫法（regex 不合就用佔位字串）。有兩個消費端：

- `_claim_action` 在 `decision.action == "needs_human"` 的補充區塊中，`extra` 含 `retry-review` 且 run 的 reason 為 `blocking-findings` 時覆寫 `next_step_hint`。它和既有的 `review-attest` 覆寫互斥，因為後者只在 `copilot-*` 時出現。
- `manager.workflow_status_entry` 在合併 `_phase_recovery_actions` 之後，若沒有 persisted hint、`reason_code == "blocking-findings"`、且 `next_actions` 含 `retry-review`，就改用本函式輸出。import 併入同一個 `try` 內既有的 `from .work_actions import _phase_recovery_actions`，失敗時照舊退回通用提示。

manager.py:12697 的 rejected 分支不改。在讀取時計算，已經卡在 blocking-findings 的舊 run 也拿得到新提示；hint 只在對應動作確實被宣告時才出現，與 #382 的原則一致。

### D8 `review-attest` 以結構標記拒絕

在 `_review_attest_action` 中，`_validate_current_run_authority(active, authority, run)` 之後、`foreign = ...` 前置檢查與任何 GitHub 讀取之前，加入判定：

```python
if any(step.phase == "review" and step.gate_result == "needs_human" for step in run.steps):
    raise RuntimeError(...)
```

判定依據是持久的 step 標記，而不是 `needs_human_reason`：舊版 `review-attest` 已經清掉 facet 的 run，在這裡同樣會被擋下。`review-attest` 只寫 maintainer evidence，不會改變 review step 的判定，所以 rejected 的 review gate 必須由 `retry-review` 或 `retry-build` 解除。`_load_work_run` 可能補寫 delivery journal 的初始列，這是既有的冪等行為，不算本 action 的 side effect。

### D9 CLI help 只改 coordinator 的 `--reason`

`cortex work retry-review` 走 `paulsha_cortex/coordinator/cli.py` 的 `work` parser，其 `--reason` help 目前只列 abandon 族（最多 500 字），與 retry-card／retry-build／retry-review 的 4000 字裁決語意不符，改寫成 spec R8 的兩組描述。其餘不改的部分：

- `paulsha_cortex/cli.py` 的 `_WORK_HELP`：retry-review 那行「以 exact Candidate CAS 只重跑 foreign review，不重跑 builder」仍然正確。
- porcelain `run.py` 的 `--reason`：本來就沒有 help。
- `control/contract.py`：對 retry-review 只驗 `expected_candidate`，不擋 `reason`。

### D10 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| reason 落地 | `tests/test_work_actions_retry_invalidation.py` 的 `_authority`／`_make_run` 樣板，run 在 review phase，review step 為 `needs_human`，`needs_human_reason=fixture_needs_human_reason("blocking-findings", ...)`，帶 plan authority，`execute_work_action(action="retry-review", reason=...)` | evidence 檔存在且唯讀；body 的 card 等於 rejected 卡、phase 為 review、reason 已 strip；回傳 `adjudication_evidence.ref` 指向該檔；review step 回到 pending，needs_human 已清除 |
| 非法 reason 無 side effect | 同上，reason 為 `"   "`／`"x"*4001`／`123` | `ValueError`；run 的 facets 與 step 不變；`evidence/operator-adjudication` 不存在 |
| reset 失敗不留孤兒 | 合法 reason，`expected_candidate` 不符 | `RuntimeError`；沒有 evidence |
| 無 reason 回歸 | 既有 retry-review 測試 | 原斷言全綠；`adjudication_evidence is None` |
| 裁決進 reviewer prompt | 真 writer → `manager._operator_adjudications` → `manager._workflow_job_prompt`（review／reviewer step） | 短 reason 逐字出現；`OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE.strip()` 在 prompt 中；2500 字 reason 的 evidence body 保留全文，prompt 只含前 2000 字、不含尾段標記 |
| directive 漂移 | 讀常數 | 每個 blocking 與 non-blocking 類別名都在 reviewer directive；builder directive 沒有該句；#814 的 byte-identical 測試仍綠 |
| 曝光面 | `_phase_recovery_actions`，換 reason／active job／`verified_head`／plan authority；另用 `list_jobs()` 拋例外的 registry stub | blocking-findings 時兩者都宣告且 retry-review 在前；`list_jobs()` 失敗時兩者都不宣告；其他情形依 spec R5 的矩陣 |
| hint | `workflow_status_entry(registry, run)`；`execute_work_action(action="resume")`（`tests/test_reviewer_card_retry_569.py` 的 resume 樣板） | `next_actions ⊇ {abandon, retry-review, retry-build}`；`next_step_hint == blocking_findings_next_step_hint(...)` |
| review-attest 假成功 | review step 為 `needs_human` 的 run，monkeypatch `GitHubDeliveryClient`（`tests/test_work_actions.py` 的 review-attest 樣板） | `RuntimeError` 且訊息含指定字串；沒有 `evidence/maintainer-review`；`gate_refs`／`facets` 不變；既有 review-attest 測試仍綠 |
| copilot-* 不受影響 | `tests/test_copilot_review_adopt_existing.py` | 原斷言全綠 |
| CLI help | coordinator `_build_parser()` 找到 `work` 子 parser 的 `--reason` action；`python3 -m paulsha_cortex.cli work retry-review --help` | help 含 `retry-review` 與 `4000`；exit 0 |

### D11 Sizing

Production 模組共三個：`work_actions.py`、`manager.py`、`coordinator/cli.py`（只改 help），因此 `domain_breadth=1`。state 方面，`retry-review` 在既有的單一 registry reset 之後多寫一筆 content-addressed 的 immutable evidence 檔；不新增 registry 欄位，也沒有跨物件 CAS 或 crash consistency 要處理，因此 `state_consistency=1`。三件套齊全時機械三維固定為 4，總分 6，屬 Yellow。
