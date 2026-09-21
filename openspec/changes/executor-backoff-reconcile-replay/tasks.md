---
status: accepted
work_item: executor-backoff-reconcile-replay
domain_breadth: 0
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# Executor backoff 對帳 pending 重播補 ack 與 workflow decision consumer 工作清單

## Tasks

- [x] **T1 tests／RED**：新增 `tests/test_executor_backoff_reconcile_replay.py`，以 fresh `JobRegistry`、terminal inventory、空／部分 store 與 workflow resume harness，逐條釘住 spec R5 (a)–(e)；現行 production 必須維持 RED。
- [x] **T2 source／重播（R1、R2、D1、D2）**：admission 對 PENDING identity 以既有 `record_backoff` 重播 missing inventory events，單次重做 reconciliation；完成後走既有 eligible／skipped 判定，store 拒絕則維持 unknown。
- [x] **T3 source／診斷（R3、D3）**：unknown diagnostics 加 pending／event epoch／replayed 計數與 replay diagnostics，並記錄每次重播結果。
- [x] **T4 source／poll 消費端（R4、D4）**：workflow post-advance dispatch result 經 `classify_dispatch_result` 投影；decision 原樣放入 `dispatch_decision`，不讀 `job_id`。
- [x] **T5 tests／回歸**：既有 executor backoff、workflow lane、dispatch contract 與 provider recovery tests 維持全綠，補真 cooldown 與第二次 admission 不重播斷言。
- [x] **T6 documentation／changelog／CLI help**：同步 changelog、lifecycle docs 與既有 CLI help smoke；明列本票 merge 後才可升含 #928 的 pin。
