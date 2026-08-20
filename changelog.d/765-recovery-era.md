# 765-recovery-era

- **`#765` 補遺：recovery 選擇器（最後一個 era-blind）同樣以 claim era 過濾。**
  operator_resume 的 recovery 分支把前代 era 的已綁 evidence job 抓回重放，
  advance 的 binding 對現 era 必炸——實機 verification-38 每次 resume 被重放，
  正是 enriched diagnostics 抓到的確定性簽名的出處。
