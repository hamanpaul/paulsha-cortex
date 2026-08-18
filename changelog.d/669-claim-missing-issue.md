# 669-claim-missing-issue

- **`#669` 修正：claim 判定 `missing_issue` 不再建立 run**——舊行為是「先建 run 再宣告
  blocked」（run 的結構化理由逐字寫著「claim 判定需要人工介入即建立 run：missing_issue」），
  自我託管首輪掃描因此在八秒內產出 **24 個內容完全同型的 `needs_human` 殭屍 run**，全部停在
  `current_phase: claim`、`gate_state: running`、`evidence_refs: []`、`next_actions: []`，
  永遠不會推進。根因是類別錯誤：`missing_issue` 對 `docs/superpowers/workstreams/*` 這類
  work item 而言**是預期狀態，不是異常**（`cost-governance-cluster/todo.md` 開頭逐字寫著
  「本 workstream 不對應單一 issue」），系統卻把它物化成 durable state，`attention` 的信噪比
  被壓成 1:24——唯一真正需要人看的 blocker 被埋在 24 筆噪音底下。`_claim_action` 現在回
  `action: not_claimable`／`run: None`，**一次都不呼叫 workflow starter**。
- **`#669`：跳過必須可查詢，不得把 fail-loud 換成 fail-silent**——只是「不建 run」會讓真的
  漏開 issue 的 work item 被靜默略過，噪音變盲區、方向反而是錯的。新增
  `coordinator/not_claimable.py`：耐久 ledger `<coordinator_root>/not-claimable.json`
  （schema `cortex-not-claimable/v1`），逐筆記 `reason`／`detail`／`source`、
  `first_observed_at`／`last_observed_at`／`observations`（卡多久、被判過幾次）、
  `authority_digest`、`mapped_openspec`／`mapped_todo_paths`，以及可照抄執行的
  `next_step_hint`。`cortex status` 新增獨立的 `not_claimable` 區塊（`attention` 因此只留
  可行動的項目）、文字模式逐筆印出理由與下一步、`cortex digest` 帶計數。work item 一旦重新
  可 claim，下一次判定即自動清掉該筆——不留永久假警報。
- **`#669`：修正前留下的殭屍 run 帶著清理指令浮現，但不由系統自行清除**——沿用「auto-claim
  不得自動清除或重試 `needs_human` run」的守衛（`#373`）。這批 run 有唯一可機械辨識的簽名
  （ongoing ＋ `claim` phase ＋ `needs_human` facet ＋ 理由為
  `claim-blocked`／`work_bridge.start_workflow_for_authority` ＋ 零 evidence／PR），命中時
  claim 回 `reason: claim-blocked-stale-run`、附 `stale_run_id`、`legal_next_steps:
  ("abandon",)` 與完整的 `cortex work abandon … --expected-run-id …` 指令，實機清理由
  operator 明示執行。簽名逐項為必要條件，測試逐欄位釘住——停在 build／verify／review 的
  `needs_human` run（握有真正工作成果）不會被誤判成殭屍。
- **`#669` 測試**：新增 `tests/test_claim_not_claimable_669.py`（14 項），涵蓋
  「`missing_issue` 時 starter 一次都不得被呼叫」「registry 與 delivery journal 零殘留」
  「ledger 可查且 `observations` 不會每輪長出新列」「`cortex status`／文字模式看得到」
  「**真的該建 run 的情況仍然建**」「issue 補上後 ledger 自動清空」「殭屍 run 帶清理指令」
  「真正卡住的 run 不被誤分類」「ledger 對損毀 fail-closed、但呈現面不得因此死掉」。
