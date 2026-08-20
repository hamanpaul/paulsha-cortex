# 765-journal-claim-rebase

- **`#765` 補遺：delivery journal 的 authority rebase 連 `claim_key` 一起帶。**
  journal 停在建列時代的 claim 使 delivery/resume 的 canonical 視圖與 run row
  分屬兩個 era——下游 job 選擇撿到舊 era terminal、binding 每 tick 必炸（實機：
  run row 5a1615 vs journal 5929，resume 永遠 mismatch）。
