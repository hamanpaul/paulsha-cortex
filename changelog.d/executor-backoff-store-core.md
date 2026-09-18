- **#850 executor backoff store core**：擴充既有
  `paulsha_cortex/coordinator/executor_backoff.py` 與
  `tests/test_executor_backoff.py`，把最初的 strict-reader 骨架補成單模組
  `executor-backoff/v1` store：
  strict three-state reader、scope validation、stable root-scoped `flock`、fresh
  read-modify-write、temp fsync＋atomic replace＋directory barrier、exact
  terminal-key ack ledger、caller-supplied outcome payload fingerprint／authority／
  evidence ref／parsed reset metadata 驗證、fixture inventory reconciliation seam
  （`unverified`/`pending`/`complete`/`unknown`）、bounded `bounded-ledger/v1`
  容量檢查，以及依 event-time 穩定排序的 immutable fold。active clear 會以
  `active-cooldown` 拒絕，expired replay 保持冪等且不釋放 ledger 額度；production
  caller inventory sourcing 與 workflow/slice lane 接線仍由 #825 後續 child 承接。
