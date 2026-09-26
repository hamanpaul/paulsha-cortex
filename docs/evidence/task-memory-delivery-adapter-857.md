# Issue #857 本地驗收證據

日期：2026-09-26。範圍為目前 worktree 的 Cortex consumer、Manager receipt boundary、sidecar 與唯讀 read model。此紀錄不代表 commit、PR、merge、installed service 或 live provider 已交付。

## RED → GREEN

- 初始 RED：先建立 `tests/test_task_memory_adapter_857.py`，adapter 模組尚不存在時，指定 pytest 執行在 collection 階段因 `paulsha_cortex.coordinator.task_memory` 無法 import 而失敗。
- Capability regression RED：暫時停用 capability mismatch guard 時，`test_contract_capability_mismatch_fails_closed` 失敗，實際結果錯誤成 `offered`（預期 `read-failed`）；恢復 guard 後同一測試 `1 passed`。
- Provider/canary RED：新測試首輪抓到 live-canary per-path aggregation 的 `KeyError`（把 aggregate row 當成 repo map）；修正後加入 provide-run 級 5/5 gate，避免單次多 note 回應冒充多次成功。另修正 CLI family 在 registry reload 後的動態註冊。
- 對抗審查 MAJOR RED（2026-09-26）：inline-only `context-delivered` 仍被當成通用成功，且 canary 沒有獨立 retrieval gate；新增 inline-only regression，另新增 help usage regression，初跑兩者皆失敗（retrieval/delivery 分類缺失、usage 出現重複 `canary`）。修正後兩項均 GREEN。
- GREEN：指定 adapter/provider/Manager dispatch/CLI 測試 `83 passed, 1 skipped`；加入 porcelain registry/bootstrap/run 順序驗證後 `134 passed, 1 skipped`。唯一 skip 為 `PSC_TASK_MEMORY_HIPPO_CMD is not set; live Hippo CLI integration was not requested`。
- 完整 repository suite（最後 Manager prompt-order 微調之前）：`83 failed, 6922 passed, 99 skipped, 186 subtests passed`。失敗集中在此 sandbox 的 AF_UNIX bind `Operation not permitted` 與遞迴 POSIX ACL `setfacl: Invalid argument`。最後微調後指定 task-memory/Manager/help 組重跑通過；task-memory 全量案例在完整 suite 中也通過。
- Hippo fixture 依已發布 `hippo/task-memory/v1` builder/validator 形狀：schema version、intent、candidate `ref/rank/summary/authorization/availability`、delivery capability、evidence、producer/adapter identity；另測 unknown optional fields。manifest、note id/hash/version 是 adapter 所需的 optional extension。僅有 core envelope 時，本地 consumer 以 `manifest-mismatch` fail closed。

## Issue acceptance 對照

| AC | 本地自動化證據 | 狀態 |
| --- | --- | --- |
| AC1 accepted planning 與唯一 work item | `test_accepted_planning_set_is_registered_once_and_pins_hippo_dependency` | 已完成。 |
| AC2 provider/schema fail-safe，不污染 lifecycle/KPI | `test_provide_exit_codes_map_to_bounded_adapter_diagnostics`、`test_manager_dispatch_is_byte_identical_when_flag_is_off_and_provider_absent`、既有 unsupported-schema 與 legacy-output tests | 本 repo 已完成。需 live 驗收 Hippo 缺席／Trust Root 讀不到 memory root；canary 命令見下方。 |
| AC3 delivery、manifest-bound fetch、receipt、冪等 | 既有 delivery/receipt tests、`test_fetch_closure_binds_the_validated_manifest_and_rejects_outside_note`、`test_manager_dispatch_opt_in_uses_provider_and_records_cortex_sidecar` | 本 repo 已完成。真 service dispatch receipt 的 live read-model 回查仍需驗收。 |
| AC4 每條 path 5/5 與 ≥95% gate/blocker | `test_inline_context_delivery_does_not_pass_content_retrieval_gate`、`test_live_canary_cli_emits_bounded_machine_json_and_private_evidence`、`test_live_canary_rejects_fewer_than_five_successful_provides_per_repo_path` | note-fetch 成功為 hash 相符的 `content-returned`；snapshot 成功為內容讀回且 hash 相符（`snapshot-ready` 不算）；inline `context-delivered` 獨立列 delivery，`counts_as_read=false`。eligible authorized retrieval rate 僅計 note-fetch/snapshot 且須 ≥95%；每個 repo/path 至少五次成功 provide 是另一個 gate。假 Hippo 本地驗證已完成，live 數值仍需真實 provider 驗收。 |
| AC5 跨 repo/task kind、permission-denied | 既有 scope/permission tests，加上 live canary permission 與 cross-project controls | Local fake-provider coverage 已完成；真實 Hippo registry 與 OS permission 需 live 驗收。 |
| AC6 read model 含 work/run/job/routing/plan/receipt/test/review 與 blocked 明示 | 既有 read-model/blocked-state tests、Manager sidecar dispatch test | Projection 與 Manager sidecar 本地已完成；需 live `cortex work show --task-memory` 驗收真實 run/job/receipt。 |

Live 驗收命令（以 Hippo registry 中兩個 canonical repo 取代範例值）：

```bash
cortex task-memory canary --repo <hippo-registered-repo-a> \
  --repo <hippo-registered-repo-b> --runs 5 \
  --evidence-path "$HOME/.agents/core/runtime/task-memory-canary-857.json"
```

通過條件為每個 repo/path 至少五個 eligible authorized provide 與五次完整 delivery、provide 與 candidate 成功率 ≥95%、預設 `--runs 5` 為 5/5、permission-denied 負例成功且無 scope/cross-project leak。JSON 不含 note 正文與本機絕對路徑。

## 驗證命令與邊界

使用 issue 指定的 Cortex `.venv` 執行。為避免把個人絕對路徑寫入 repo，以下以 `$CORTEX_PYTHON` 表示該 venv 的 interpreter：

```bash
$CORTEX_PYTHON -m pytest -q -p no:cacheprovider \
  tests/test_task_memory_adapter_857.py tests/test_task_memory_hippo_provider_857.py \
  tests/test_multi_issue_worktree.py tests/test_cli_help_alignment.py
```

指定命令（adapter/provider 加 Manager dispatch/CLI help）：`83 passed, 1 skipped in 10.19s`。含 porcelain registry/bootstrap/run reload 順序驗證組：`134 passed, 1 skipped in 12.38s`。完整 repository suite 實跑：`83 failed, 6922 passed, 99 skipped, 186 subtests passed in 288.86s`；此 run 在最後 Manager prompt-order 微調前完成，微調後指定組仍通過。代表性限制為 AF_UNIX socket 建立回傳 `Operation not permitted`，`setfacl` 在 pytest 暫存檔與工作區回傳 `Invalid argument`，使依賴 socket/ACL 的既有 sandbox、Trust Root 與 dispatch 測試失敗。新增的 task-memory tests 在全量執行中通過。PR-context policy/CI、live Hippo CLI 與 installed service canary 未執行。

代表性重現命令：`pytest tests/test_afunix_sun_path_608.py::test_a_real_bind_succeeds_under_a_hostile_tmpdir tests/test_builder_tasks_tick_verify_dispatch.py::test_checkbox_only_tick_clears_verify_dispatch_end_to_end tests/test_inner_sandbox_714.py::DegradedLaunchTests::test_output_last_message_is_a_sibling_of_the_job_log`。分別收到 AF_UNIX `Operation not permitted` 及遞迴 `setfacl` `Invalid argument`；後者阻斷依賴 ACL 的 dispatch/degraded-launch setup。全量失敗涵蓋相關既有 socket、ACL、degraded launch/Trust Root 案例；新增 task-memory 測試在全量執行中通過。新增行與新檔案的個人路徑/使用者名稱掃描無命中。`git diff --check` 通過；未執行帶 PR 上下文的 policy/CI。

Hippo #155 production provider/CLI 尚在另一 repo 的未 merge worktree；Cortex 依其 CLI 契約實作 client，不 import Hippo、不改 Hippo、不寫 strict ledger。`PSC_TASK_MEMORY_ENABLED` 預設關閉；測試證明 provider 缺席時 prompt byte-identical，並以 `provider-unavailable` 留下 Cortex sidecar receipt。fake CLI 覆蓋 exit codes、timeout、output bound、stderr injection、manifest membership 與 Cortex hash check。需執行上方 live canary，再於安裝環境用 `cortex work show --task-memory` 回查真實 run/job/receipt；本次未執行真 Hippo CLI。

### 2026-09-26 對抗審查 MAJOR 修正與重驗

- 成功事件依 path 明列於 JSON evidence：`note_fetch` 使用 `content-returned`（adapter 已驗證 note content hash）；`snapshot` 使用 `content-returned`（必須讀回 snapshot 內容且 hash 相符，只有 `snapshot-ready` 不計讀取）；`inline` 使用 `context-delivered`，標記 `metric_kind=delivery`、`counts_as_read=false`。
- `content_retrieval` 的分子與分母只加總 `note_fetch`、`snapshot` 的 eligible authorized candidates；`inline_delivery.delivery_rate` 獨立輸出。Canary 通過需同時滿足 aggregate content retrieval ≥95%、每個 repo/path 至少 5 次成功 provide 且成功率 ≥95%、scope 與 permission 負控制；inline delivery 不會補 retrieval 分子或分母。
- RED→GREEN：inline-only regression 初跑失敗，修正後 `test_inline_context_delivery_does_not_pass_content_retrieval_gate` 通過；help regression 先確認輸出錯誤為 `cortex task-memory canary canary`，修正 parser `prog` 後不再重複。
- 指定 task-memory 命令：`71 passed, 1 skipped in 5.36s`。Porcelain CLI help：`10 passed in 0.28s`。唯一 skip 為未設定 `PSC_TASK_MEMORY_HIPPO_CMD`，故 live Hippo integration 未執行。
- 本次只改 worktree，未 commit、push、開 PR 或部署。真實 Hippo provider／installed service canary 仍待執行。
