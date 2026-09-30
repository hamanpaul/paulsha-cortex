### Fixed

- **#716 canary 診斷涵蓋模板 job 的 log spool**：模板 unit 派出的 job 把 log 寫在各 principal 的 job log spool（builder `commit-spool/build-logs/<i>/`、reviewer／planner `review-verdicts/planning-logs/<i>/`，#708），不在 `logs/workflow/`；run 36656955386 因此一份 job log 都沒印到。診斷改為同時掃這些位置，取最近 3 份（#716）。
