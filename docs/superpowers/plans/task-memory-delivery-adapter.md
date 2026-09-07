---
status: accepted
work_item: task-memory-delivery-adapter
---

# Task memory delivery Cortex adapter Plan

## 1. Authority and dependency

- [ ] 將 Cortex #857 與 Hippo #146 綁為跨 repo dependency；Hippo 是 generic contract owner，Cortex 是首個 host adapter。
- [ ] 將本 plan、spec、design、workstream Todo 與 `.cortex/work-items.yaml` registration 保持同一 work item，讓 isolated worker 不依賴暫存檔。
- [ ] 確認本次只走現行 `cortex work intake`/Manager single-writer；不使用 deprecated low-level dispatch、不直接寫 durable registry/ledger。

## 2. Contract and RED coverage

- [ ] 凍結 `hippo/task-memory/v1` envelope、0–3 candidate、manifest、delivery modes、receipt/event names 與 strict KPI separation；由 Hippo #146 的 public contract 驗證，不在 Cortex 複製第二份權威 schema。
- [ ] 新增 Cortex adapter contract tests：Work Item/WorkflowRun/card → task envelope，缺 task id、跨 project、manifest/hash mismatch、unsupported major、ineligible source 均 fail closed。
- [ ] 新增 receipt tests：inline/context-delivered、readonly snapshot/content-returned、manifest-bound note-fetch、permission denied/read-failed、applied-with-evidence 分流與 retry idempotency。
- [ ] 先取得真正 RED（測試確實能抓到 adapter 未接線或錯誤分類），保存 bounded test evidence；不得以文件 checkbox 代替 RED。

## 3. Adapter implementation

- [ ] 在 Cortex thin adapter 接上既有 Work Item/WorkflowRun/card task identity 與 executor capability；不改 central routing、trust-root、shared quota 或 Hippo lifecycle owner。
- [ ] 依 capability matrix 實作 primary delivery mode 選擇與 explicit fallback/ineligible reason；拒絕 arbitrary host path、global memory-root access、allow-all。
- [ ] 以 Manager-owned sidecar/receipt boundary 接回 attempt/run，不由 worker 直接寫 Hippo ledger；receipt/hash 不符時保留 fail-closed attention。
- [ ] 沿現有合法 routing；不以 model override 修改全域 default，不將任何 executor/model 名稱寫成 special case。

## 4. Canary and utility readiness

- [ ] 每個支援 delivery path 以獨立 memory root、受限 headless executor 及固定 fixture 執行至少 5 次成功與負例。
- [ ] Cortex 固定 denied path 重現後，驗同一 generic payload 可取得允許內容；另以不同 repository/task kind 通過，證明沒有 Cortex project branch。
- [ ] 機械計算 eligible authorized content retrieval success rate，目標 ≥95%；列出 unknown/ineligible/denied，不以 126 intent 或 299 denied view 推算 adoption。
- [ ] 只在 canary gate 通過後設計 task-level control/treatment utility trial；retries 合併、至少明列實際 evidence、成本與誤引用，未完成不宣稱 utility improvement。

## 5. Verification and delivery gates

- [ ] Cortex focused tests、Hippo contract fixture/subprocess boundary、完整 tests、policy check（PR context）、`git diff --check` 全部記錄實際輸出。
- [ ] 檢查既有 strict `offer → read → applied` 分母與輸出 schema 未改；inline/context delivery 不混入 legacy Read。
- [ ] 以正式 Cortex read model 回查 work item/run/job state、plan/spec revision、model identity、receipt/sidecar、test/review evidence；issue open、accepted plan、queued、blocked、started、complete 必須分開報告。
- [ ] 遇 provider stale、project registration、authority、permission、quota 或 sandbox blocker，停在精確 `needs_human`/blocked，不修改全域設定、不手造 evidence、不回收 unrelated work。
