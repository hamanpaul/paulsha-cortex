### Added
- **Issue #275：發布 canonical engineering outcome contract 供外部 learning systems 消費**：新增
  `paulsha_cortex/coordinator/engineering_outcome.py`——append-only、一 repo 一檔的
  `engineering-outcomes/<repo-slug>.jsonl` outbox，`work_actions._ship_action`／
  `_abandon_action` 在既有的 `status="done"`／`status="superseded"` terminal transition
  之前 durable 寫入一筆 `shipped`／`abandoned` record（`outcome_id` 由 run_id／outcome／
  該次轉換的內容位址 digest 決定性推導，daemon restart 或 request retry 重複 tick 不會
  產生第二筆）。record 含 per-job `card`／`persona`／`workflow_phase` 展開欄位，
  `execution_provenance` 誠實標示 `correlation_confidence: "weak"`（job record 目前沒有
  存 executor 自身 session UUID，只有 worktree-path＋時間窗可用）。`rejected`／`failed`／
  `rolled_back` 是 schema 保留值，v1 沒有對應的 run-level 終局轉換點可掛，尚無 emitter。
  新增 `cortex outcome list/show/replay` 唯讀 CLI surface。設計決策見
  `docs/superpowers/specs/engineering-outcome-contract-{spec,design}.md`。
