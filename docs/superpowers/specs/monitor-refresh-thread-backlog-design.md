---
status: accepted
work_item: monitor-refresh-thread-backlog
---

# Monitor refresh 排程與 publication 設計

## Decisions

### D1 單一排程邊界，不同來源各自保留真相

資料流：watcher／poll／rescan／GitHub timer → bounded dirty map → 公平 ready queue → 至多 W 個 worker → generation/source revision 檢查 → snapshot commit/event publish。
worker 是服務擁有的固定資源；watcher 不再為事件啟動 Timer。dirty map key 使用 canonical project/source；未知 workspace 事件歸全量 scan source，不走同步旁路。全量與局部 pending 合併時不得漏掉其覆蓋外 project。
每 key 保存首次 pending 時刻、最新 generation、active/dirty 狀態。連續事件只更新 dirty/version，不刷新首次 pending；達 D 即 ready。active 結束後若 dirty 則至多重新排一份，新的 ready 項排在已有 ready 項後。

### D2 公平與有限等待的前提明示

同一輪已就緒來源數為 Q，各工作服務上界為 L；W=1 的保守開始上界為當前 active 剩餘上界 + Q×L，另計尚未 ready 的 D。W>1 仍可用此保守界，不宣稱真網路每次固定延遲。
GitHub provider scan 不得在持有 publication 全域鎖時佔住整段 I/O；可以在鎖外建候選結果、在 commit 時檢查 generation/revision 並合併仍有效的來源狀態。測試 slow fake GitHub 時 local 可按有限界開始，或在無可用 worker 時明確呈現等待/timeout，不製造成功時間。
新 local publication 與舊 remote 結果的衝突以 source revision 驗證/重新合併裁決；不可不檢查而整包覆寫，也不可讓持續 local churn 永久丟棄 remote 更新。若現有 provider adapter 不具有限 timeout，R7 的殘餘有界/診斷成立，但 R3 的一般有限進度尚未成立，需回主流程補依賴。

### D3 寫入者與停止線性化

服務/scheduler 擁有 lifecycle generation；work refresher 擁有 durable snapshot/read model commit。stop 與最後 generation check/commit 需共用同一 fencing 協議；publication 已在線性化點完成者算 stop 前，stop 已生效後的候選一律不得 commit。
server.publish_events、publish_work_events 與 durable publish 均列入 fence 測試。停止順序為 reject → invalidate → cancel pending → stop watcher → join 合作式 worker → report residual；多次 stop/unwatch 不重複發布、不新增 worker。
不以丟掉 Future/thread 引用宣稱停止；stuck worker 仍計入 W。不可取消外呼是明示 bounded residual，若要更強終止保證需另處理 adapter/process boundary。

### D4 診斷與測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| A burst／無界 worker | Event 擋住第一輪、100 次 submit | active≤W、A pending≤1；放行後最多兩次且 snapshot 最新 |
| Debounce starvation／A 壓住 B | fake clock、固定來源順序 | 首次 pending+D 後 ready；B/GitHub 不被後來 A 插隊 |
| 慢 provider／舊結果 | 可 timeout/cancel fake provider + revision barrier | queue-wait/provider-timeout 具體診斷；不假更新 freshness、不覆蓋新 authority |
| Stop／已開始 callback | commit 前後兩個 barrier、durable bytes/hash spy | stop 生效後零 commit/event；合作式 S 內退出、pending=0 |
| 不合作外呼／資源殘餘 | 受控 stuck fake、finally 放行 | owned worker≤W、stuck 可見、無補開 thread、遲到零 publish |
| Wiring／usage drift | 既有 monitor fixture + 短 real-thread smoke | rename/rescan/last-good 原斷言不變，結束回 owned-thread 基準 |

各等待皆 bounded 且 finally 放行；真 sleep 不作主要同步。原 issue 的全域 active_count 只作輔助指標，主要斷言 component-owned worker/pending。

### D5 Sizing 與交付

三個 production 模組 → domain_breadth=1；concurrency/atomicity → state_consistency=2；8 條 requirements 對應 invariant_count=8。W/D/S 由實作的可注入常數與文件明示，驗收測試固定實際數字及推導界，不用 host 速度硬猜。
現行完整 fix-standard lane 預期 9/Red；即使 #831 改正 stability 分量，仍不能未經實跑就當 Yellow。拆分候選為 bounded scheduler/coalesce、work-model 公平 publication/stop 接線兩個有獨立測試的單元；只是後續規劃方向，不是已授權 child authority。
