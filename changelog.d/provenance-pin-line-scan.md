# provenance-pin-line-scan

- **#466 實跑驗證的 follow-up：deck 指紋的 provenance pin 改行掃描**——A-3 原實作把整份
  `provenance.yaml` 餵零依賴 subset YAML parser，但真 deck 的 provenance 含多行自由文字
  欄位（`variant_notes` zh-TW 折行），超出 parser 子集，`cortex model profile` 對真
  pilot-v1 直接 fail-closed 誤觸（`unexpected indentation`）。改為行掃描機器寫入、格式
  固定的頂層 `content_sha256: <64 hex>` pin 行（恰一行才收，容許引號），已對真 deck
  全 8 關驗證；fixture 加入多行折行欄位鎖回歸。
