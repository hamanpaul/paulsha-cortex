### Added
- **Issue #279：跨 repo ad-hoc 一次性派工——設計文件（design-doc）**：新增
  `openspec/changes/2026-08-07-design-adhoc-oneshot-dispatch/`（proposal／
  design／tasks／`specs/trusted-dispatch-completion/spec.md`）與
  `docs/superpowers/specs/adhoc-oneshot-dispatch-{design,spec}.md`，定案
  D1-D6：`cortex run once` 繞過 control queue、直接組裝既有
  `JobRegistry`/`Dispatcher`/`manager.run_tick()` 於呼叫行程內完成派工，
  job 狀態落 ephemeral tmp 路徑與宿主 `~/.agents` 物理隔離（不擴充
  `PSC_INSTANCE`/`_installed_environment()` 機制）；repo-root 沿用既有
  `_infer_repo_root()`，worktree／branch 建立行為不變，「呼叫方既有
  branch/worktree 內工作」明確列為 v1 非目標；combo 重用 #324 的
  `small-fix`，不新增更輕量 combo（`validate_manager_spine()` 七 phase
  涵蓋為不可放寬的治理憲法）；builder identity 臨時放行透過既有
  `load_model_identities()` 的 packaged+instance-local 合併機制，不改
  registry 驗證邏輯。另發現 `depends_on` 列的 #338（persona catalog gate
  對外部 repo 派工必炸）症狀已由 #341（commit `0264f3f`，早於本票查證
  基準 main）解掉，判定其現況為「症狀已消失、issue 未關閉」。本票不動
  任何 `paulsha_cortex/` 程式檔；code 落地拆為四張候選後續票（見
  `tasks.md` 文末拆票建議）。
