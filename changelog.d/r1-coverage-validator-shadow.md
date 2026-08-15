### Added
- **v4 R1（方案 A）：responsibility coverage validator 的 shadow 骨架——零行為變更**
  ——v4 的核心論點是「safety truth 不該依賴 `current_phase`（workflow topology），而該
  依賴責任覆蓋（responsibility coverage）」。本 PR 是這個重構的**觀測期**第一步：新增
  `paulsha_cortex/coordinator/coverage.py`，把新的 coverage validator 與現行 topology
  validator（`WorkflowManifest.validate_manager_spine()`，一 byte 未動）**並行跑、比對、
  記 telemetry**，但 production 決策**仍完全由舊 validator 主導**。新增 `SafetyStage`
  列舉（七個 safety responsibility，與七 phase 一一對映；`INTAKE`/`DELIVERY` 為
  Manager-owned authority boundary）、`ResponsibilityCoverage` 結構、legacy
  `phase → responsibility` adapter，以及 deck card schema 的 optional `satisfies` 欄位
  （capability declaration，**非** self-certification；現有 deck 不需改，由 adapter 從
  phase 兜底）。shadow 掛在 `manager.py` 的 production 派工 gate（`_manager_workflow`
  start）呼叫點旁，**全程 try/except、永不 raise**，且受 `PSC_RESPONSIBILITY_COVERAGE`
  閘控（`off` 連比對都不跑，預設 `on`）。disagreement telemetry 以一次比對一檔的原子
  寫入落在 `coordinator_root()/coverage-shadow/`（比照 D4 event-spool 語意），含 manifest
  識別、兩方判定、disagreement 細節，供兩週觀測期分析。**零行為變更**：測試證明無論
  coverage validator 判 pass 或 fail，`validate_manager_spine()` 的結果（pass/raise 與
  原因訊息）與本 PR 前逐位元組相同，manifest 序列化 bytes 不含 `satisfies`；shadow 只
  多寫 telemetry。不做 Compact reuse／auto_develop／monotonic escalation（R2）、不碰
  GitHub 邊界／trust-root／採信契約。新增 `tests/test_coverage_shadow_r1.py`（26 個測試）。
