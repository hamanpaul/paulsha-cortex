---
type: fix
scope: coordinator
---
**Issue #519：semantic-reclaim 世代熔斷補上帶審計的重置路徑**

`coordinator/work_actions.py` 的 `_claim_action` 在 `#218 AC2` 對同一
`(repo, work_id)` 累積 `SEMANTIC_RECLAIM_LIMIT`（3）個 `superseded` 世代後熔斷
為 `needs_human: semantic-reclaim-budget-exhausted`。計數是對**全部歷史**無條件
累加：不看時間窗、不看失敗原因、不看引擎是否已修好，而且完全沒有重置路徑，CLI
也沒有 force／override。檢查點位在 `decide_auto_claim`／`decide_manual_start`
之後、`workflow_starter` 之前，auto 與明示 `work start` 共用——結果是根因修好之
後 work item 仍永久鎖死。實測（2026-08-14 dogfooding）`fix-brainstorm-revalidation-diagnostics`
在四分鐘內因三個 cortex 自身缺陷（成功卻被自行 supersede、codex exec 逾時、前代
殘留 artifact 使 authority fail-closed）耗盡額度，無一是工作項本身的問題；
`fix-rate-limit-classification` 的三次 abandon 同樣全肇因於當時未修的 `#507`／
`#511`／`#516`（皆已修復部署）。

本次採 `#519` 建議 4（帶審計的重置動作）：

- **新增 `reset-reclaim-budget` work action**：走既有 work-action 路徑（單一
  writer、經 manager daemon），要求 `--actor` 與單行 `--reason`，界限與
  `abandon`／`retire-delivered` 逐字相同（actor ≤128、reason ≤500、單行、可列
  印）。刻意不要 `--expected-run-id`：熔斷觸發的前提就是 `decide_*_claim` 已判
  成 claim、沒有 active run 可供 CAS，硬要 exact run id 等同讓解鎖路徑永遠打不
  開。白名單外參數一律 fail-closed 拒絕。
- **重置以 append-only 水位實作**：把當下所有未赦免的 superseded `run_id` 記成
  一筆 registry 授權列（coordinator 狀態檔新增 `reclaim_resets` 根欄位），熔斷
  計數改為「superseded 世代扣掉所有已赦免 run_id」
  （`_effective_superseded_generations()`，claim 路徑與重置動作共用同一支計
  數）。**既有 `WorkflowRun` row 一列不刪不改**——run 歷史是稽核來源，重置是新
  增一筆授權事實，不是抹掉失敗紀錄。水位以 run_id 集合而非時間戳表達，不依賴
  任何時鐘假設；重置後新產生的世代照常累加，熔斷會再次上膛，不是永久關閉安全
  機制。`_manager_record_reclaim_reset()` 拒絕赦免不存在或非 superseded 的
  run_id（不得預先授權未來的失敗），同 `evidence_ref` 重入冪等。
- **`cortex-work-reclaim-reset/v1` evidence**：落在
  `<coordinator_root>/evidence/work-reclaim-reset/{work_id}-{hash}.json`，含
  `repo`／`work_id`／`actor`／`reason`／重置前的世代數與其 run_id 清單／
  `created_at`；canonical json hash 命名、create-with-O_EXCL + hardlink + fsync
  + 0444、內容衝突 raise，全部沿用 `_write_supersede_evidence()`（新增 `stem`／
  `label`／`max_size` 參數：支援 work-item 級而非 run 級的記錄，並讓大小上界逐
  caller 指定——本 body 隨被赦免世代數線性成長，沿用 abandon 的 4096 會讓長歷史
  work item 的 byte-identical 重放被誤判成 conflict、冪等重入必然 fail）。順序
  比照 `#275`：先寫 durable evidence 再改狀態。
- **熔斷訊息指出下一步**：`semantic-reclaim-budget-exhausted` 結果新增
  `reclaim_budget_limit`／`superseded_run_ids`／`legal_next_steps`／
  `next_step_hint`（比照 `#218 AC3` 的 `legal_next_steps` 慣例），operator 不再
  只看到「額度用盡」而不知有解。
- **rate-limit 容忍**：`reset-reclaim-budget` 併入 retirement family 既有的
  `allow_rate_limited_last_known_good` 集合（`_LOCAL_UNBLOCK_ACTIONS`）——同樣只
  動本機狀態、不依賴 issue 當下 open/closed，而系統被限流的當下正是卡死 work
  item 最需要被解開的時候。

`reclaim_resets` 是**加法相容**的可選根欄位：不 bump `schema_version`，不列入
`_load()` 的 `missing_v2_roots` fail-closed 清單，本欄位出現前寫下的既有狀態檔
照常載入（缺欄位＝沒有任何重置授權，即維持熔斷最嚴格的既有行為）。
`monitor/providers.py` 的唯讀 canonical root 驗證同步接受「有」與「沒有」兩種形
狀，仍不接受任何其他未知根欄位。

**未採建議 1（納入引擎版本維度）**，理由見 PR body：`WorkflowRun` 沒有引擎版本
欄位且既有 row 無法回填、cortex 發版頻率遠高於單一 work item 燒完三代（熔斷會近
乎失效）、且「引擎已修好所以舊失敗不算數」本質上是需要具名負責的人工判斷——正
是建議 4 所提供的東西。
