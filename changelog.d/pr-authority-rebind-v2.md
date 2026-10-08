# PR authority rebind v2

- WorkAuthority claim era 驗證通過後，resume 將 active delivery journal row 的 `claim_key` 同步到 WorkflowRun，修復舊 era run 的 resume 與 review-disposition。
- 同一 candidate 執行 retry-review 後，explicit resume 會派出替代 review job；PR、issue、OpenSpec mapping 改變仍維持 fail closed。
