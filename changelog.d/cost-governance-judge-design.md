### Added
- **Issue #138：交付成本治理 judge（cost-aware dispatch + 控速分流，不擋）設計文件**：新增
  `docs/superpowers/specs/cost-governance-judge-{spec,design}.md` 與
  `docs/superpowers/plans/cost-governance-judge.md`。凍結 `rate` 自追資料契約
  （`RateSnapshot`：`available`／`tokens_remaining`／`window_seconds`／`last_429_at`），
  新模組落點 `rate_tracker.py`（不擴充 `model_identities.py`／`claim_readiness.py`／
  `manager_daemon.py`，理由分述於 design D3）；凍結控速分流層 `filter_ready()` 介面契約，
  掛點為 `autonomy.ready_units()` 與 `dispatch_ready()` 之間的新過濾步驟，並與 `#136`
  已落地的 `capacity_gate.py`（daemon-idle 布林閘）劃清「並行兩把閘、不同稀缺資源軸」的
  邊界；429 回授裁定重用 `manager_daemon._tick_backoff_seconds()` 的指數封頂**公式**、
  不重用其 daemon-level **狀態**；凍結 judge MVP 四因子合取判斷式（`rate_available ×
  quota_remaining × capable() × track_record()`），並定案四個 interim stub 契約——`#137`
  `track_record()`／`#209` `capable()` 尚未 code-landed 期間全恆真，行為與現況等價（安全
  no-op），四者可獨立分批替換為真值；串接 `#137` `session_health` opaque
  pass-through 邊界，凍結 `should_terminate()` 五類終止觸發契約（含「precompact harvest
  hook」查無實據、stall/報酬遞減本 repo 無既有機制的誠實記錄）。裁定 MVP 不新增
  `context_window`／`quota_window_kind`／`autonomy_safety_profile` 三個靜態欄位，未來
  如需新增遵循 `#209` R3 既定路徑（additive 擴充 `model-identities.yaml`，不新開
  `resource-inventory.yaml`）。查證「haman/arc 多帳號池」config 在本 repo 不存在，不予
  假設。本票不實作任何程式碼、不動 `autonomy.py`／`claim_readiness.py`／
  `manager_daemon.py`／`capacity_gate.py`、不開 `openspec/changes/**`（依 issue 原文，
  落地時再依 OpenSpec 流程另開）。同步更新
  `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 的 `#138` 列狀態。
  複驗訂正：初版誤將 `#137`（one-shot 成效閉環／track-record）標注為「已落地設計」，
  查證 main 上不存在 `#137` 的設計文件（僅存在於未合併分支
  `feature/137-oneshot-lesson-loop-design`），已訂正為「設計初稿、尚未合併」，
  design.md／spec.md／plan.md 相應處已同步更正；另補上與已落地票 `#325`（job usage
  schema）、`#324`（combo 可擴充與可選）的介面關係查證——`#325` 提供歷史 per-job
  用量記錄與本票即時 per-resource rate 閘門互補不重疊，`#324` 屬 workflow/card 骨架層
  與本票無介面耦合，兩者查證結果詳見 spec.md／design.md「與相鄰票的介面關係」段落。
