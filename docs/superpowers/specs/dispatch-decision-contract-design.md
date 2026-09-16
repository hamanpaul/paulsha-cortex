---
status: accepted
work_item: dispatch-decision-contract
---

# 派工結果契約設計

## Decisions

### D1 一個內部判定契約，多個正式消費端

在 coordinator 定義共用的 dispatch-result 分類／投影 helper，供 manager 與 daemon 使用。
實作可採帶 discriminator 的內部型別，但外部 request JSON 與 durable registry 不升 schema、不新增假 Job。
既有 dict producer 遷移期間只能以明確可驗證的形狀轉接，未知 dict 不可當成 generic success。

| 結果類別 | 必須證明 | 可回應內容 |
|---|---|---|
| 真正 Job | 有有效 job_id，且正式 registry 的 run/card 綁定相符 | exact job_id、最新 run；原有 Job status 不改判 |
| 非 Job 決策 | 當前 run/phase 的正式 dispatch decision，具 reason；沒有 Job 副作用 | reason、最新 run、適用時既有 detail；不得標 dispatched |
| 確定性 transition | 呼叫前後持久化 run 顯示合法 phase/card 推進，無 Job | 最新 run 與具體 transition，不臆測下一張模型已派出 |
| None 且無 transition | 無新 Job、無 phase 推進 | 原有 not-dispatchable／空結果語意，不 fabricated success |
| 未知或矛盾 | 無法符合以上任何一型 | fail-closed 的 contract 診斷，不吞掉真實例外 |

各正式 consumer 在派工後重新讀取該 run；不得把呼叫前的 run payload 原封回傳。
Job 判斷不只測字串存在；測試須證明 forged job_id、錯 run、malformed dict 不會獲得 dispatch authority。

### D2 Side effect 與回應分開

Claim/define/plan 可能先 durable 完成，再回傳無 Job 決策；回應層失敗不能抹掉已完成動作。
仍保留既有 claim/CAS/terminal evidence 不變量，不為修 adapter 加 transaction rollback 或重派。
真正 dispatch/launcher 例外仍照原補償；只將合法無 Job 結果從例外路徑移開。

### D3 Forced retry 保留較嚴格後置條件

forced retry 的成功後置條件是新 replacement Job。None、拆分、preflight refusal 或僅 phase transition 均不滿足。
回應保留原 decision 作診斷，補回 needs_human 時附具體 reason；不能產生 KeyError、假 job_id 或假 redispatched。

### D4 Consumer 清冊與回歸矩陣

實作先用函式引用找出 `dispatch_workflow_card`、`_dispatch_workflow_card`、`resume_workflow_run` 的 consumer，至少涵蓋：

- daemon workflow-action 同步 dispatch、work start/intake、forced retry。
- manager resume 的首派、operator recovery、provider bounded retry、terminal 後的下一張 dispatch。
- periodic workflow resume，以及它將例外轉成 needs_human 的外層。

每個適用入口以真 Job／None／非 Job／transition 測試；forced retry 另驗拒絕。
「沒有其他 consumer」只可標當下掃描佐證；上述分類不變量由測試鎖住，新增 consumer 未接契約時測試必須紅燈。

### D5 交付與 sizing

Production 至少跨 manager/daemon 兩個模組，domain_breadth=1；維持舊結果兼容與 durable workflow 狀態投影，state_consistency=1。
基底舊算法預估 8/red，保留真實宣告。先完成 #831 並以實際新 runtime 重算；若仍 Red，先拆有獨立驗收的子工作，不直接 dispatch 本整包。
不以改 combo、清欄位、調低數值、手改 frozen authority 或刪 gate 通過。
