---
status: accepted
work_item: passive-executor-health
---

# 移除主動模型健康檢查，改用 job 事件串流被動判斷存活與限流；測試套件完全密封

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1312`，含 issue 留言的不密封測試清單。
- owner 原則（2026-10-06）：健康檢查不要主動 polling，不得為了探測而送出模型請求。
- 現況：
  - `coordinator/executor_auth.py` 的 `check_executor_auth`／`cached_copilot_model_availability` 會實際執行 `copilot -p 'Respond with OK only.'`。unknown 結果不快取，所以會反覆探測。
  - copilot 撞限流（`session.error`，`errorType=rate_limit`）後會掛住不退出，只結束 node wrapper 時 native 子程序還會殘留。
  - 測試套件有 12 個檔案、41 個測試會走到真實的探測，每跑一次全套就送出 59 次真實請求。
- copilot 目前已停用（身分矩陣 park，入口有守門腳本），但修法必須對所有 executor 一致，不能只處理 copilot。
- 不改 executor 的派工語意與 independence 規則。

## Tasks

- [ ] **T1 RED**：新增 autouse conftest 守門：在 PATH 最前面放 copilot、codex、claude、agy 的假執行檔，被呼叫就記錄並讓測試失敗。現行全套至少 issue 列出的 41 個測試應失敗。
- [ ] **T2 測試密封**：讓這些測試改用替身（runner 注入或 monkeypatch），全套在守門下全部通過，CI 與本機行為一致。
- [ ] **T3 移除主動探測**：派工前的可用性檢查只看免費的訊號（CLI 是否存在、版本、登入憑證檔與 token 是否存在、已知的 backoff 狀態），不送出任何模型請求；移除或停用 `probe_copilot_model_availability` 這類探測路徑，並更新呼叫端。
- [ ] **T4 被動判斷存活與限流**：Manager 監看 job 的 JSONL 事件串流：最近一筆事件的時間，以及錯誤事件（`rate_limit`、認證錯誤、sandbox panic）。出現錯誤、或超過上限時間沒有新事件時，結束整個 process group（包含 native 子程序），記錄 outcome，並依回報的解除時間寫入 executor backoff；解除後自動恢復派工。
- [ ] **T5 清理殘留**：Manager 啟動時與每個 tick，清理本 instance 遺留、超過上限時間的 executor 探測或 job 程序，並記錄。
- [ ] **T6 文件**：operator 文件說明健康判斷方式。新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
