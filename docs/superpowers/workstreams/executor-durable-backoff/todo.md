---
status: accepted
work_item: executor-durable-backoff
---

# Executor durable backoff 最小安全層

## Boundary

- Issue：`hamanpaul/paulsha-cortex#825`。
- 本票持久化 executor×model 已知 rate-limit／quota cooldown，供 slice／workflow
  dispatch 共用，解析 reset hint 並留下 skip evidence；不新增 outcome taxonomy、
  不改 GitHub provider backoff、不新增 operator override CLI。
- 這是被動限流後的最小退避，不是完整 quota-aware routing：共享帳號／quota pool、
  任務用量預估、reservation、動態 model／agent／effort 選擇由 refine plan 後續
  工作實作。本票的 identity key 不能代表不同模型具有獨立帳號額度；#825 完成不等於
  完整第5類問題完成，也不授權降低品質或突破 pin／independence-domain。

## Tasks

- [ ] 新增 `coordinator/executor_backoff.py`，狀態位於
      `<coordinator_root>/executor-backoff.json`，schema `executor-backoff/v1`，
      key 為 executor／model_id；提供 `active_backoff`、`record_backoff`、
      `clear_backoff`，dispatch 前重讀持久狀態。以原子替換保存，故障不得遺失仍有效的
      已知 cooldown；相同 terminal job 重複觀測須冪等，不可每 tick 增加次數或延後期限。
- [ ] 缺檔表示無本機已知 backoff；損壞／不可讀／未知 schema 則回顯式
      unknown/degraded 與診斷，**不可當空檔／所有模型可用**。保留可取得的 last-good
      cooldown，但無法確定涵蓋範圍時暫停此 store 管轄範圍的新增派工；不改已執行 job。
      後續讀到有效檔可恢復，不需要消耗 provider retry count。
- [ ] deadline 優先採可信 `reset_at + RESET_MARGIN_SECONDS`，缺 hint 時沿用
      `backoff.tick_backoff_seconds` 的有界指數退避，quota base 大於 rate-limited。
      清除已過期條目不得縮短其他 identity 的期限；不同 identity 同時寫入的更新
      不可互相覆蓋，測試 stale-reader／交錯 writer 或明確 single-writer enforcement。
- [ ] 新增 `parse_reset_hint(text, *, now) -> int | None`，支援 Retry-After 秒數及
      `try again at Sep 7th 12:23 PM`；對缺時區／年份文字記錄解析所採的 operator
      timezone／基準年份，無法可靠判定或過去時刻則回 None，採本機保守退避。
      structured `resetsAt` 保有優先序，文字 hint 不提高 authority；不改既有 outcome
      vocabulary 與 `provider_outcome` 四個必要 keys＋可選 `reset_at` 的形狀。
- [ ] 以 `record_executor_backoff_from_job` 接入 slice build failed 與 workflow
      failed 終局路徑，限 `rate_limited`／`quota` 且 authority 非 hint 的證據。
      無 executor／model_id 時不捏造 key，記錄缺少 identity 的診斷供後續觀測。
- [ ] workflow `_runtime_preflight_gate` 與 `_provider_failure_reroute` 的 lookup
      對 cooldown identity 回 `ProviderFreshness(status="degraded",
      source="executor-backoff")`；所有候選 cooldown 時回 `reason="executor-backoff"`、
      `retry_after_epoch`、`skipped`，不轉 needs_human、不耗 `provider-retry:<card>`。
      store unknown 使用可區分的 reason 與診斷，不捏造 reset 時間。
- [ ] 有合格可用候選時沿既有資格／permission／pin／independence-domain 規則選擇，
      replacement job 保存 `dispatch_reroute` 的 source 與 skipped 證據；不得為繞過
      cooldown 放寬原有 gate，也不能把相同共享帳號池的其他模型宣稱為額度獨立。
- [ ] slice `autonomy.dispatch_ready` 在 `_record_pending_slice`／建立 worktree 前
      查詢 backoff；identity 採 spec 的 executor／model_id，否則使用 launcher 的
      公開 executor／model property（為 model 新增公開 property，不讀 private `_model`）。
      命中時保留 dispatchable，不建立 job／worktree，不轉 failed／needs_human。
- [ ] `dispatch`／`retry-build`／`fanout`／`tick` 回傳
      `dispatch_skipped_by_backoff: [{slice_id, executor, model_id, retry_after_epoch}]`；
      unknown store 另附診斷且不填假 deadline。`retry-build` 不拋
      `retry-build-dispatch-failed`；dispatch request 必須先檢查空 dispatched list，
      不再直接取 `dispatched[0]`。
- [ ] 新增 `tests/test_executor_backoff.py`／`tests/test_reset_hint_parsing.py`：
      record→active→expire、reset 優先序、quota base、指數上限、重啟讀取、同 job
      重複觀測冪等、損壞／權限／schema unknown、原子寫入故障、交錯更新；reset hint
      覆蓋明確 timezone、跨日／跨年、過去／不合法、Retry-After 0 與負值。
- [ ] 新增 workflow／slice lane 回歸測試：fresh `JobRegistry` 且清
      `_EXECUTOR_AUTH_CACHE` 後仍跳過、期限屆滿後可派、全候選 cooldown 不增加 retries、
      corrupt store 重啟後不 launch、未知恢復後可派、無空列表 IndexError／無多餘 worktree。
      保留既有 provider backoff／failure recovery／outcome 測試原始斷言。
- [ ] 補本 workstream changelog fragment／`CHANGELOG.md [Unreleased]`，透過 Cortex
      完成 RED／GREEN、獨立 review、完整 gates、merge 與 restart-safe runtime 驗證；
      另以後續 quota work item 承接本票明確未交付的主動預估與共享池能力。
