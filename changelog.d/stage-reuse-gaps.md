# stage-reuse-gaps

- **`#844` reused receipt 連結來源並在採信點重驗（G844-2／S11）**：`resume_workflow_run()`
  不再於任何驗證之前預寫 `decision=reused`（舊寫法在 evidence 缺檔／壞 hash／模型明示停止等
  分支失敗時，run 上會留下一張沒發生過的 reuse）。probe 判定 reused 後只記下來源 job，由
  `apply_workflow_action(action="advance")` 以自己從 registry 重讀、之後受 revision CAS 保護的
  同一份 run 快照呼叫 `_stage_reuse_adoption_receipt()` 重驗（provenance、candidate、claim-era、
  planning authority、test policy、builder job、gate ledger、operator 裁決），通過才把連結
  `source_run_id`／`source_claim_key`／`source_job_id`／`source_evidence_path`／
  `source_evidence_hash`、key／schema／`compatibility` 與 `adoption`（accepted／rejected）的
  receipt 與 gate 同一次寫入。決策與採信之間的 drift 擲 `StageReuseAdoptionDrift`，記
  `ineligible`／`adoption-drift` 後結束這次接續、不落 needs_human，下一次接續重新裁決（S07）。
- **`#844` probe 驗 provenance（S05）**：key 相同之外，來源 job 的 receipt 必須是現行 schema、
  逐欄與現在的快照相同、且 job 實際記錄的 executor／model 就是快照上的 identity；只抄一把
  相同 key 的 job 或舊 schema receipt 一律 stale、改派新 attempt。registry 讀到其他 key schema
  版本的 job receipt 只驗最小形狀，不再讓整份狀態檔 fail closed（S10）。
- **`#844` 交錯重複 request（S08）**：另一個 request 已採信同一張 verify／review 卡後才到達的
  resume，回 `duplicate-continuation`，不再把健康的 run 標成 `workflow-advance-failed`。
- **`#844` authority restart 列出失效範圍（S09，2026-09-20 留言）**：一般 resume 因 authority 前進
  讓已接受的 verify／review gate 回 pending 時，`cortex work resume` 結果帶
  `stage_invalidation`（原因、前後 claim key／source revision、candidate、需重跑與保留的卡），
  同一次寫入把這些卡的 receipt 標成 `ineligible`／`authority-restart`。
- **`#844` 呈現（S11）**：resume action result 帶 `stage_reuse`；`fresh` receipt 帶新 attempt 的
  `job_id`；強制新 attempt 派不出去時 receipt 照實記 `ineligible`。`cortex work show [--json]`
  經 Monitor workflow provider 的 `stage_reuse` observation 呈現每張卡的 receipt。receipt 新增
  `receipt_schema_version=2`，第 1 版 receipt 照舊可讀、不回填；目前部署的 runtime pin 讀新欄位
  不 fail closed（rollback 測試）。
- **`#844` 文件（G844-3）**：新增 `docs/stage-evidence-reuse.md`，列出 card 層支援 cohort、receipt
  欄位、migration 與未支援範圍（build／planner／manager 卡、跨 run／claim-era 採信），供 #829
  總帳引用，並附 S12 live canary 的安排步驟。
- **`#844` 測試**：新增 `tests/test_workflow_stage_reuse_adoption_844.py`（S01 正式 work-action →
  daemon production executor 入口、evidence 由 production harvest 產生；S03／S04 逐項不相容；S05
  偽造／他 run／非零 exit／撤銷／缺檔／壞 hash／symlink／越界；S06 review 卡；S07 採信前 drift；
  S08 crash 點、重送、交錯雙 request、延遲 terminal；S09 retry-card、active attempt、authority
  restart；S10 build 卡、舊 schema、無 job 的 ineligible；S11 呈現、相容與 CompletionRecord
  `reused_from`）。
