---
status: accepted
work_item: fix-mutation-request-timeout
---

# fix-mutation-request-timeout Design

## Decisions

### D1 分級 timeout 表 + pending 語意

引入 `_REQUEST_TIMEOUTS: dict[str, float]`（key=req_type：`fanout`/`tick`=60、`complete`/`work`/`run`=30、缺省 5）。`_submit_mutation_request` 依 req_type 查表取 timeout。timeout 路徑回傳「submitted-but-not-done」結果（含 req_id + 追蹤指引訊息），CLI 層 exit code 用新常數 `EXIT_SUBMITTED_PENDING`（區別 `EXIT_FAILURE`）。保留既有成功/失敗路徑。

### 不選全非同步 submit

現有 operator 慣例為 `--wait` 同步等（quickstart `cortex run tick --wait`）；改全非同步會破壞契約。分級 timeout + 明確 pending 訊息即可消除「誤以為失敗→重試→撞 worktree already exists」連鎖。