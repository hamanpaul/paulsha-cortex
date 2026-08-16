### Added
- **R1 shadow telemetry 的 aggregation reader ＋ TTL retention（R1 Go/No-Go 的直接輸入）**
  ——PR #590 落地了 coverage validator shadow 的 sink（一次比對一檔），但沒有讀端；
  R1 的 Go/No-Go 判準是「兩週 telemetry 中所有 disagreement 可解釋」，沒有統計就無從
  判讀。本次於 `paulsha_cortex/coordinator/coverage.py` 新增**唯讀** aggregation reader
  `build_shadow_report()` 與 on-demand CLI
  `python -m paulsha_cortex.coordinator.coverage --report [--json]`（比照
  `python -m paulsha_cortex.trust_root ...` 的模組入口慣例，不動 `cortex` 傘狀 CLI）：
  輸出總筆數、agreement／disagreement 計數與比例、觀測窗（earliest/latest）、
  disagreement 依 `kind` 分組（理論上只有 `topology-fail-coverage-pass`），每組附
  combo／task_slug／callsite／missing-responsibility 的分佈計數與逐筆樣本明細
  （含 `context` 與 `satisfied_by`，足供人工逐筆解釋）。`--json` 輸出結構化報告
  （`schema_version` 獨立於單筆 telemetry 的 schema），`--samples N`（`0`＝全部）
  控制樣本數。
- **shadow telemetry 的 TTL 清掃**——新增 `DEFAULT_SHADOW_TTL_SECONDS`（預設 30 天），
  比照 D4 event spool 的 `DEFAULT_EVENT_TTL_SECONDS` 慣例：**只在 reader 執行時順帶
  清**，不加任何 daemon 常駐邏輯。以記錄的 `recorded_at` 判齡，缺漏或解析不出來
  （含壞檔）時降級用檔案 mtime，讓壞檔也會隨時間退場而非永久堆積；被清掉的記錄不
  計入統計母體。`--ttl-days N` 可調、`--no-sweep` 純唯讀。刪不掉（唯讀掛載、或
  trust-root Phase 2 之後 Manager-owned 樹對 operator 唯讀）時只計入 `sweep_failed`
  並照常出報告，不 raise。
- **壞檔容錯**——單筆 JSON 讀不到／不是 JSON／不是 object 一律跳過並計入 `corrupt`，
  絕不炸掉整份報告；記錄內部欄位型別歪掉（`manifest` 不是 mapping、`recorded_at`
  不是字串……）亦逐欄降級為 `-` 而非例外。掃描端跳過 dotfile，與 sink 的
  `.coverage-*.tmp` 半寫入檔約定閉合。
- 新增 `tests/test_coverage_shadow_reader_591.py`（27 個測試）：統計正確性（分組、
  分佈、樣本、觀測窗、樣本上限）、TTL 清掃（過期清除、邊界、自訂 TTL、`--no-sweep`、
  mtime 兜底、清掃失敗只計數）、壞檔容錯、與真實 sink 的端到端、CLI 文字／JSON 輸出
  與參數驗證。
- **範圍**：只做 reader ＋ retention。#591 其餘項（`satisfies` projection 進 manifest、
  雙 legacy phase 對映收斂、`work_bridge.default_workflow_manifest()` 第二呼叫點
  儀器化）屬 R2，本次不做；telemetry 的寫入端與 `validate_manager_spine()` 一 byte 未動。
