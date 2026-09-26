#600 overlay 選出的 Copilot 模型會在 dispatch 建立 Job 前接受限時可用性探測；明確不可用時依既有候選 reroute，CLI 無法判定時記錄診斷並照常派工；明確結果在 Manager 行程內以 900 秒 TTL 快取，避免每次派工都消耗 Copilot 請求。
