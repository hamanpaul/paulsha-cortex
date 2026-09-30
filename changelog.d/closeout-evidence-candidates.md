### Fixed

- **#716 canary 結案的 evidence candidate 綁定**：driver 原本要求每個 verify／review job 的 evidence candidate 都必須等於 workflow 最終的 candidate。但 ship 的 archive commit 會換掉 candidate，archive 之前那一輪 verify／review 驗的是舊 candidate，因此被誤判成 `workflow evidence candidate mismatch`。現在每張卡的 evidence 改為綁定該 job 自己的 `subject_head`；另外要求最終 candidate 必須至少有一個 verify job 和一個 review job（#716）。
