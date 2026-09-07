---
status: accepted
work_item: task-memory-delivery-adapter
---

# Task memory delivery Cortex adapter 設計

## Decisions

### D1 Hippo owns generic contract; Cortex owns translation

Hippo #146 定義 `hippo/task-memory/v1` 的 task envelope、candidate 與工具中立 receipt。Cortex adapter 只把 Work Item/WorkflowRun/card 的穩定欄位、當次 execution profile 的 capability 宣告與既有 read scope 映射進 envelope，再將返回 receipt 綁回同一 attempt。不得把 Cortex internal schema 暴露給 Hippo。

### D2 Delivery is an explicit capability matrix

Adapter 先計算 `eligible`、允許 evidence sources 與 capability，再選擇 inline、task-scoped readonly snapshot 或 manifest-bound note-fetch。每次只允許一種 primary delivery mode，receipt 另記 fallback/拒絕原因。工具不可用不改成 arbitrary path；host permission denied 不改成 allow-all。

### D3 Evidence is append-only sidecar, not central ledger mutation

Worker/isolated executor 只產 receipt/sidecar；Cortex Manager 收集並以既有 single-writer lifecycle 綁定 work/run/attempt。Hippo 後續 consumer 可用 public receipt replay；Cortex 不等待 Hippo acknowledgement，不代 Hippo 寫 `read`/`applied`。

### D4 Strict KPI and new acquisition metrics stay separate

Legacy strict funnel 只接受原本合法的 Read/Applied adapter evidence。新 receipt 以獨立版本化 projection 報告 `authorized_delivery_success_rate`、attempt/return/failure/ineligible 與 evidence-backed applied；inline 不進 legacy Read。

### D5 Generic canary before utility trial

第一階段只驗授權內容取得、歸因與 isolation。canary 的 task fixture 必須有 Cortex 以外 repository/task kind；permission-denied 是 bounded negative control，不從單筆或 126 個歷史意圖推算 utility。第二階段才做 task-level control/treatment，retries 合併計算。

### D6 No shared routing mutation

沿現行 Cortex routing；不新增 global model override、不變更 trust-root、service env 或 shared quota。若現行 routing/資格無法滿足，保留 fail-closed blocker。

## Data flow

```text
Work Item/WorkflowRun/card
  -> task envelope + capability/read scope
  -> Hippo candidate request (public v1)
  -> capability matrix
       | inline -> context-delivered receipt
       | snapshot -> readonly manifest + content-returned receipt
       | note-fetch -> manifest-bound tool call + return/failure receipt
       | ineligible/failure -> bounded reason receipt
  -> Cortex sidecar/single-writer projection
  -> Hippo public consumer / legacy KPI separation
```

## Failure rules

- task identity 不完整：保留 `session_proxy` 或拒絕，不假造 task。
- candidate hash/version 或 manifest 不符：拒收，不嘗試 arbitrary path。
- host/tool permission denied：記 `read-failed` 與 bounded layer-unknown reason；不可聲稱已定位 root cause。
- task 不允許額外歷史 evidence：記 `ineligible`，不可 inline 偷塞。
- Hippo/provider unavailable：Cortex execution lifecycle 照既有 fail-safe；receipt 可 park/replay，不阻塞 unrelated completion。
- schema major 不支援：park，保持舊 strict KPI 與既有 output schema。

## Acceptance oracle

所有產出都須可由 task id、attempt id、note hash、manifest hash 與 receipt chain 重建。`authorized_delivery_success_rate` 的分母只含公開 eligibility 規則下的 authorized tasks；unknown、probe、供應資料不足與 no-extra-evidence 類別分開列出，不靜默丟棄。
