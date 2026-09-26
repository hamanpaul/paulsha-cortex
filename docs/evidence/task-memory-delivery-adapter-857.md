# Issue #857 本地驗收證據

日期：2026-09-26。範圍為目前 worktree 的 Cortex consumer、Manager receipt boundary、sidecar 與唯讀 read model。此紀錄不代表 commit、PR、merge、installed service 或 live provider 已交付。

## RED → GREEN

- 初始 RED：先建立 `tests/test_task_memory_adapter_857.py`，adapter 模組尚不存在時，指定 pytest 執行在 collection 階段因 `paulsha_cortex.coordinator.task_memory` 無法 import 而失敗。
- Capability regression RED：暫時停用 capability mismatch guard 時，`test_contract_capability_mismatch_fails_closed` 失敗，實際結果錯誤成 `offered`（預期 `read-failed`）；恢復 guard 後同一測試 `1 passed`。
- GREEN：最終相關測試合併執行為 `230 passed, 5 skipped, 4 deselected`；task-memory 專屬檔共 36 tests。後續 capability、scope、receipt order、snapshot permission、blocked read model 與 legacy output regression 都納入套件。
- Hippo fixture 依已發布 `hippo/task-memory/v1` builder/validator 形狀：schema version、intent、candidate `ref/rank/summary/authorization/availability`、delivery capability、evidence、producer/adapter identity；另測 unknown optional fields。manifest、note id/hash/version 是 adapter 所需的 optional extension。僅有 core envelope 時，本地 consumer 以 `manifest-mismatch` fail closed。

## Issue acceptance 對照

| AC | 本地自動化證據 | 本地狀態／外部條件 |
| --- | --- | --- |
| AC1 accepted planning 與唯一 work item | `test_accepted_planning_set_is_registered_once_and_pins_hippo_dependency` | 已完成；註冊與 accepted 文件都在 repo。 |
| AC2 provider/schema fail-safe，不污染 lifecycle/KPI | `test_missing_provider_and_unsupported_schema_park_without_lifecycle_mutation`、`test_core_hippo_payload_without_adapter_manifest_extension_is_parked`、`test_contract_capability_mismatch_fails_closed` | 本地已完成；production provider 未接線，需 live 驗收缺席時的既有 dispatch 行為。 |
| AC3 四類 delivery、receipt、冪等 | `test_delivery_paths_emit_bound_receipts_and_keep_inline_out_of_read`、`test_note_fetch_is_manifest_bound_and_never_accepts_a_host_path`、`test_snapshot_is_sealed_manifest_bound_and_symlink_replacement_fails_closed`、`test_receipt_sidecar_is_deduplicated_append_only_and_contains_no_note_body`、`test_manager_rejects_out_of_order_or_unreturned_task_memory_receipts`、`test_inline_applied_receipt_requires_confirmed_delivery_and_artifact_evidence` | 本地已完成；Manager 以 registered Job worktree 的 no-follow 唯讀 SHA 驗證 applied artifact。正式 dispatch receipt harvest 需 live provider/runtime 接線。 |
| AC4 每條 path 5/5 與 ≥95% gate/blocker | `test_each_canary_path_has_five_positive_and_permission_negative_controls`（inline/snapshot/note_fetch）、`test_canary_scope_relay_and_legacy_schema_observations_are_blockers` | 離線 fixture 各 path 正向 5/5；安裝後的 eligible retrieval ≥95% 仍需 live 驗收。 |
| AC5 跨 repo/task kind、permission-denied | `test_adapter_has_no_project_or_task_kind_special_case`、`test_permission_denial_is_bounded_and_does_not_claim_a_permission_layer`、`test_snapshot_permission_denial_stays_unknown_and_bounded`、`test_scope_manifest_and_hash_mismatches_fail_closed_without_returning_content` | 本地 fixture 已完成；真實 permission layer 未被推定。 |
| AC6 formal read model 含 work/run/job/routing/plan/receipt/test/review 與 blocked 明示 | `test_formal_read_model_joins_work_run_routing_plan_receipts_and_gate_evidence`、`test_work_show_task_memory_is_separate_versioned_read_model`、`test_read_model_keeps_blocked_and_undispatched_state_explicit` | fixture projection 已完成；live Monitor/Manager state 回查需在安裝環境驗收。 |

## 驗證命令與邊界

使用 issue 指定的 Cortex `.venv` 執行。為避免把個人絕對路徑寫入 repo，以下以 `$CORTEX_PYTHON` 表示該 venv 的 interpreter：

```bash
$CORTEX_PYTHON -m pytest -q -p no:cacheprovider tests/test_task_memory_adapter_857.py
```

相關測試另含 `test_cli_help_alignment.py`、`test_cli_entry.py`、`test_monitor_work_api.py`、`test_workflow_run_forward_compat_1042.py`、`test_engineering_outcome.py`、`test_workflow_registry.py` 與 `test_workflow_production_wiring.py`。production-wiring 中 4 個需遞迴 ACL 的既有測試在 sandbox 出現 `setfacl: Invalid argument`；該環境限制下已排除這 4 個案例，其他案例仍執行。`git diff --check` 通過；新增行與新檔案的個人路徑/使用者名稱掃描沒有命中。完整 repository suite 與帶 PR 上下文的 policy/CI 不是本次 local-only worktree 驗收結果。

Hippo #146 的 published helper 提供 payload builder/validator，沒有 production retrieval provider 或 note-fetch callback。Cortex 不 import Hippo package、不改 Hippo、不寫 strict ledger。`cortex work show --task-memory` 只查詢現有 sidecar；未帶旗標的 `cortex-work/v1` 維持原格式。正式 provider 接線、Manager live harvest 與 installed/live canary 是後續必要步驟。
