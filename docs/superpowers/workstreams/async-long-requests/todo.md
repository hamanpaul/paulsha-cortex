---
status: accepted
work_item: async-long-requests
---

# 耗時 request 不再卡住 daemon，CLI 各入口對忙碌狀態一致

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1307`。
- 現況：Manager daemon 同步處理 control request。`regenerate-gates`（在 daemon 內跑全套 pytest，約 19 分鐘）與 ship 前的 `preflight_ci`（10 分鐘以上）進行中時，`status.json` 不更新（`degraded: stalled`）、其他 request 全部排隊、job 結束後無法即時收割。CLI 的 `work rechain`／`work resume` 在 stalled 時拒絕，`run work retry-build` 卻照樣排隊，造成順序錯亂。
- 不改各 request 的語意與結果，只改執行方式與排程。

## Tasks

- [ ] **T1 RED**：測試耗時 request 進行中，daemon 無法更新 status、無法處理其他 request（以假的耗時 gate 模擬）。
- [ ] **T2 背景執行**：regenerate-gates、preflight 與其他會跑測試或外部工具的 request，改為背景 worker 或 job 執行；daemon 主迴圈維持回應，status 回報進行中的長時間工作與開始時間。同一個 run 的長時間工作互斥，結果回寫時仍走 CAS。
- [ ] **T3 CLI 入口一致**：所有會修改狀態的 CLI 入口，對 daemon 忙碌或 stalled 採一致規則（都排隊並保證按送出順序處理，或都拒絕），並在輸出說明。
- [ ] **T4 測試與文件**：新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
