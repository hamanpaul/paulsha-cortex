### Fixed

- **#716 validator 接受 agy smoke 的 persisted_variants 證據**：canary run 37331921480 顯示 driver（commit a6340da2 起）為 agy smoke 改以持久化對話的模型變體證明 model／effort，`raw_evidence.providers.agy` 多寫一個 `persisted_variants` 欄位；validator 原本對每個 provider 都用同一組固定欄位名單驗證，agy 這個額外欄位被當 unknown field 擋下，使部署 canary 在派工與結案都通過後卡在驗證。validator 改為對 agy 額外接受 `persisted_variants`，且要求其值恰好等於 `["<runtime_model>-<runtime_effort>"]`（與 driver 判定通過 agy smoke 的條件一致）；其他 provider 的欄位集合維持不變（#716）。
