此 draft OpenSpec change 對應 issue [#1078](https://github.com/hamanpaul/paulsha-cortex/issues/1078)，唯一 `work_item: monitor-trusted-freshness`。它規劃在 #1077 durable attempt-generation ledger 上，綁定 exact correlation input、同代 provider/source revisions 與 durable WorkSnapshot read-back，再提供一個唯讀 trusted freshness API。

依賴順序為 #1063 → #1077 → #1078 → #1064 → #1065 → #1054。此 change 只定義 #1078 producer slice；不實作 #1077 writer、#1065 WorkAuthority consumer 或 #1054 Manager admission。本輪沒有產品 code 或 formal Cortex intake。
