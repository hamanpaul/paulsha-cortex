# 765-retry-card-era

- **`#765` 補遺：`retry-card` 的 target-jobs 選擇同樣以 claim era 過濾。**
  authority restart 後前代 era 的 job（含綁定 evidence）是歷史稽核列——拿它們當
  「已採信不可重派」的拒絕理由會讓新 era 的重派無解（實機：verification-38 的舊
  era evidence 擋死 retry-card，run 卡在 verify 永不推進）。與 #766 的 advance
  過濾同一條判準。
