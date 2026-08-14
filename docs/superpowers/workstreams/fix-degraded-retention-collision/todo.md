---
status: accepted
work_item: fix-degraded-retention-collision
---

# fix-degraded-retention-collision Todo

`#523`：degraded 保留分支的 ownership collision，以及讓它永不自癒的死鎖。

## 已完成

- [x] `lifecycle.py` 保留分支剝除已被本輪認領的 source；原本有 source 而全數被認領者整筆丟棄
- [x] 原本就沒有 source 的 previous item 維持既有保留語意（不誤傷僅由 workflow_run 衍生的項目）
- [x] `work_api.py` projection 驗證失敗時降級保留上一版 projection，但讓 provider 觀測落地
- [x] 降級時把失敗原因寫入 provider `diagnostics`，不得無聲降級
- [x] 測試涵蓋：歸屬轉移不重複、`WorkSnapshot` 建構通過、部分轉移保留其餘 source、
      無 source 的 previous item 仍保留、非 degraded 時行為不變、projection 失敗不丟棄 provider 觀測

## 待辦

- [ ] refresh 連續失敗需在 `cortex status`／`doctor` 有明確訊號——目前唯一線索在
      `journalctl -u <instance>-monitor`，且被「看起來很正常的 snapshot」掩蓋
      （全部 provider 的 `last_attempt_at` 同時凍住是唯一可觀測徵兆）
- [ ] 檢視是否還有其他會產生 ownership collision 的歸屬轉移路徑（本次只修了
      fallback → declared 這一條；降級保護是縱深防禦，不是免死金牌）
- [ ] snapshot 只在整輪 refresh 結束才落地，診斷可整整落後一個 refresh 週期；
      考慮 refresh 進行中先落 `in_progress` 標記或逐 provider 增量落地
