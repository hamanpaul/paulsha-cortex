- **#961 OpenSpec 遠端 archive authority reconciliation**：`coordinator/claim.py`
  現在會先收集每個 semantic authority key 的所有 observed values，僅在同一個
  `openspec:{repo}:{ref}` 同時看到本機 `active` 與 GitHub terminal `archived`
  source，且每筆 confirmed PR 都有唯一、`merged_with_merge_commit is True` 的
  remote ancestry 證據時，才收斂成 archived；其餘衝突維持既有
  `AuthorityValidationError` fail-closed，且既有 non-reconcilable semantic
  conflict 不會再被後續 malformed semantic source 覆寫。同步補齊
  deterministic／fail-closed regression tests 與 unified work lifecycle 文件說明。
