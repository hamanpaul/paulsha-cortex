### Added
- **Issue #260：新增 `recover-repair-commit` work action**：repair job 以非 0 exit
  code、`failed` 或 exited/0 但 terminal payload 缺漏／malformed 終止，卻已在既有
  builder worktree 留下合法 descendant commit時，可經此窄化入口以雙 CAS
  （`expected_run_id`＋`expected_candidate`）確定性地 bind 為新 candidate。判準
  全部取自系統事實——worktree 取自 failed job row、HEAD 精確比對、乾淨度、
  descendant lineage 與 WorkAuthority 授權皆由 Manager 側重新驗證，caller 參數
  只做交叉比對；不啟動任何 model session。成功後寫入 immutable
  `cortex-work-repair-adoption/v1` evidence 並登錄一筆沿用既有欄位集合的
  adoption job row，`_manager_adopt_repair_candidate`（`registry.py`）原子完成
  candidate 換綁、final build card 標 passed、phase 進 verify；重送相同 request
  回報 `already-recovered`，不產生第二次 adoption。`retry-build` 的 exact
  `expected_candidate` CAS 與既有 exited/0 unbound 窄化入口原封不動。

### Fixed
- **Issue #260：resume／dispatch 不再重選 stale failed job**：`resume_workflow_run`
  的 replacement 判定與 `_dispatch_workflow_card` 的 `retryable_latest` 過去只認
  `status == "failed"`，repair job 以非 0 exit code 正常終止（`status ==
  "exited"`）時第一次 operator resume 只會重新回報 stale job 的 `job-failed`，
  要再執行一次才 dispatch replacement。新增 `_is_stale_terminalized_failed_job`
  把「`exited` 且 exit code 非 0」併入既有判定，第一次 resume 即 dispatch
  replacement；exited/0 的既有三條路徑（unbound terminal recovery、malformed
  schema retry、正常 terminalize）條件式不動。失敗回報同步附掛
  `_terminal_parse_diagnostics` 的唯讀 `terminal_diagnostics`（observed
  HEAD／job id／失敗原因），不因此授予任何 candidate authority。
