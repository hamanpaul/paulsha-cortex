---
status: accepted
work_item: feat-slice-executor-model
---

# feat-slice-executor-model Specification

#294：executor/model 只能在 fanout 層指定，異質 executor 的 plan 被迫切成多個 specs-dir，dependency graph 隨之分裂；且 `depends_on` 指向批外（跨 dir 或根本不存在）的 slice_id 完全 fail-silent——slice 靜默永不派工、無任何訊號。本票讓 spec frontmatter 宣告 per-slice `executor`/`model_id`（宣告值經 model-identities registry fail-closed 驗證）、為批外 `depends_on` 提供顯式分類診斷，並收口 fanout/tick request 層 builder identity 不經 registry 驗證的缺口（#276 同場發現）。

## 背景

`cortex fanout`/`cortex tick` 的 `--executor`/`--model` 是 request 級旗標（`paulsha_cortex/coordinator/cli.py:91-105`、`:161-188`），整批 ready slices 共用同一個 launcher——`dispatch_ready`（`paulsha_cortex/coordinator/autonomy.py:404`）只收單一 `launcher` 參數。spec frontmatter 的欄位白名單（`autonomy.py:115-123` `_normalize_frontmatter` 的 `allowed`，與 `paulsha_cortex/deck/schema.py:12-20` 的 `EMITTED_FRONTMATTER_FIELDS` 以 `tests/test_deck_contract_alignment.py` 雙向等式互鎖）不含任何 identity 欄位——宣告 `executor` 即 `unknown frontmatter key`（`autonomy.py:127`，#294 實測）。因此「同一 plan 內不同 slice 用不同 executor/model」只能切多個 `--specs-dir` 分開 fanout。

分裂後 `depends_on` 跨 dir 的邊落入無診斷區：`detect_cycles`（`autonomy.py:316`）對不在本批 metas 的 dep 直接跳過（`:333-334`）、`ready_units`（`autonomy.py:351`）只問 `is_satisfied`（`:369-371`），而滿足性判定經 handoff manifest 以 slice_id 全域定位（`paulsha_cortex/coordinator/completion.py:577` `Path(handoff_dir) / f"{slice_id}.json"`），本來就跨 dir。結果是「合法跨 dir 依賴（未完成）」與「打錯字的不存在 slice_id」可觀測行為 100% 相同：held reasons 一律 `deps-unsatisfied:<id>`（`paulsha_cortex/coordinator/manager_daemon.py:240-242` `_held_reasons`），typo 讓 slice 靜默永不派工。

另一側，workflow（work item）路徑的 identity 一律經 model-identities registry 選擇與驗證（`paulsha_cortex/coordinator/manager.py:5271-5302`；#205 run-scoped override 對 unknown identity fail-closed 並列可用 candidates，`manager.py:5278-5283`），但 fanout/tick/dispatch request 與 periodic tick 的 builder identity 直接 `_resolve_launcher(executor, ..., model=...)`（`manager_daemon.py:665-670`、`:700-705`、`:853-858`）——`--model` 原樣進 executor argv，不查 registry。這是 #276 的同場發現，於本票收口。

## Goals

- 單一 specs-dir 可容納異質 executor/model：per-slice 宣告覆寫 fanout 層預設，dependency graph 保持完整。
- 宣告的 identity 必須是 registry 已知身分：unknown 即 fail-closed 並列出可用清單，不靜默退回預設（比照 #205 D4 語意）。
- `depends_on` 的 typo 不再 fail-silent：批外引用依「有無 handoff trace」分類為 external／unknown，與 in-batch 未完成明確區分。
- fanout/tick request 層明確宣告的 builder identity 同樣經 registry 驗證，關閉 workflow 路徑與 slice fanout 路徑之間的驗證不對稱。
- 未宣告 per-slice identity 的既有 specs 行為位元不變。

## Requirements

### R1 spec frontmatter 支援 per-slice `executor`/`model_id` 成對宣告

`parse_spec_frontmatter`／`_normalize_frontmatter` SHALL 接受 optional `executor` 與 `model_id` 兩個 frontmatter 欄位，值 MUST 為非空字串。兩欄 MUST 成對出現：僅宣告其一 MUST 產生 `invalid-frontmatter` parse_error 且 field 指向缺漏的那一欄，MUST NOT 默默補值或忽略。未宣告時 parse meta MUST 帶 `executor: None`／`model_id: None`，其餘欄位（`dispatch`/`slice_id`/`plan`/`depends_on`/`target_branch`/`verification`/`parse_error`）的解析行為 MUST 位元不變。

`deck/schema.py` 的 `EMITTED_FRONTMATTER_FIELDS` MUST 同步納入兩欄（維持 `tests/test_deck_contract_alignment.py` 對 parse meta keys 的雙向等式）；deck compile 的 `_render_frontmatter`（`paulsha_cortex/deck/compile.py:287`）MUST NOT 開始輸出這兩欄——deck 產物維持 identity-agnostic。

### R2 宣告的 identity MUST 通過 model-identities registry 驗證且 unknown fail-closed

`dispatch_ready` 派工前，宣告的 `(executor, model_id)` MUST 存在於 `load_model_identities()`（`paulsha_cortex/coordinator/model_identities.py:240`，packaged＋instance custom 合併）之中。unknown identity MUST fail-closed：該 slice MUST NOT 建 worktree、MUST NOT 啟動任何 model session，slice 標 `needs_human`，且錯誤訊息 MUST 列出 registry 目前可用的 `executor/model_id` 清單（比照 #205 D4／`manager.py:5278-5283` 的可用 candidates 訊息語意）。MUST NOT 靜默退回 fanout 層預設 identity。

單一 slice 的 identity 驗證失敗 MUST NOT 影響同批其他 slice 的派工：錯誤沿用 `dispatch_ready` 既有 per-slice 錯誤隔離收進 `DispatchReadyError`（`autonomy.py:495-502`），成功的 jobs 照常回傳。

### R3 覆寫 MUST 實際生效於 dispatch；未宣告 slice 行為位元不變

宣告通過驗證時，該 slice 的 launcher MUST 以宣告 identity 構建：model MUST 進入 job dispatch argv（`_ARGV_BUILDERS` 各 builder 的 `--model` 附掛，`paulsha_cortex/coordinator/launcher.py:620-625`、`:443-444`），job row 的 `executor`/`model_id` MUST 記錄宣告值（經 `LaunchHandle` 與 `registry.attach_launch_handle`，`autonomy.py:689-707`）。builder persona 的 commit-required 轉換（`autonomy.py:439-442` 的 `as_commit_required`）與 request 層 `allow_unsafe` 語意 MUST 對 per-slice launcher 一體適用；`--allow-unsafe` 的單一 canary 限制（`coordinator/cli.py:37-47` `_refuse_unsafe_fanout`）MUST NOT 因覆寫放寬。

未宣告 `executor`/`model_id` 的 slice MUST 沿用呼叫端傳入的預設 launcher，dispatch 全路徑（prompt、pin、worktree、dispatch_head、handoff）MUST 與現行為位元一致。

### R4 批外 `depends_on` MUST 有顯式分類診斷

依賴診斷 MUST 三分：dep 在本批 metas 內但未完成 → `deps-unsatisfied:<id>`（維持現行字串）；dep 不在本批、但 handoff 目錄存在 `<id>.json` manifest → `deps-external:<id>`；dep 不在本批且無任何 handoff trace → `deps-unknown:<id>`。`cortex status` 的 `held[].reasons` MUST 呈現此分類；`cortex ready` 對含 `deps-unknown` dep 的 slice MUST 於 stderr 印出顯式診斷行，stdout 的 JSON 輸出與 exit code MUST 不變。

`detect_cycles` 對批外邊 MUST 維持「不算環」的既有判定（跨 dir 先宣告、後補 handoff 是合法時序；#294 場景 3 證明滿足性判定本來就跨 dir），`ready_units` MUST NOT 因 `deps-unknown` 拒絕整批——fail-silent 的解藥是可觀測診斷，不是新 hard gate。

### R5 fanout/tick 明確宣告的 builder identity MUST 過 registry 驗證

fanout／tick／dispatch request（`manager_daemon.py:360` `build_request_executor`）與 periodic tick（`manager_daemon.py:749` `build_periodic_tick_runner`）在「executor 與 model 皆為明確字串值」（含 daemon default 帶入）時，MUST 於派工前以 `load_model_identities()` 驗證 `(executor, model)` 存在；unknown MUST fail-closed——request 路徑回 error、periodic tick 記 tick error 且本輪不派工——錯誤訊息 MUST 帶可用 identity 清單，且 MUST NOT 啟動任何 model session。

model 未指定（None）時 MUST 維持現行為：不做 registry 驗證，executor 白名單仍由 `_ARGV_BUILDERS` choices（CLI 層）與 `SubprocessLauncher` 建構子（`launcher.py:644-645`）把關。`review_executor`/`review_model` 不在本 Requirement 範圍。

## 非目標

- 不採 #294 期望選項 2（`--specs-dir` 可重複／多 root）：per-slice 宣告落地後，單一 dir 即可容納異質 executor，多 root 的主要動機消失；殘餘的跨 dir 情境由 R4 診斷收口。
- 不動 `verification.py` 的 persona-scope 概念區與 persona catalog 來源邏輯（#295／fix-persona-catalog-portability 已於 W1 規劃並將先行變更該區；本票不改 catalog 讀取、scope 判定與 `verification.py:284-294` 的 persona-scope check 要求）。
- 不做 builder 逐 plan-Task 分段派工（#276 主體仍留 #276）；本票只收口其同場發現的 identity 驗證缺口。
- 不動 workflow（work item）路徑的 identity 選擇與 #205 run-scoped model chain override（`manager.py:5271-5302` 原樣）。
- deck compile 不輸出 `executor`/`model_id`；deck combos 不新增 task_type → identity 映射。
- `deps-unknown` 不升級為 `ready_units`/`detect_cycles` 的 hard refuse。
- 不驗證「model 未指定、僅 executor」的 request 路徑（維持向後相容）；不改 `--review-executor`/`--review-model` 驗證。

## 驗收面

- 單一 specs-dir 內兩個 slice 各宣告不同 `executor`/`model_id`，一次 fanout 各自以宣告 identity 派工；覆寫值可在 job dispatch argv 與 job row 的 `executor`/`model_id` 稽核。
- 宣告 unknown identity 的 slice fail-closed：不啟 session、標 `needs_human`、錯誤列出可用 identity 清單；同批其他 slice 照常派工。
- `depends_on` 打錯字在 `cortex status` held reasons 顯示 `deps-unknown:<id>`；跨 dir 合法依賴顯示 `deps-external:<id>`；in-batch 未完成維持 `deps-unsatisfied:<id>`。
- fanout/tick request 帶 registry 沒有的 `(executor, model)` 對時被拒並列可用清單；不帶 model 的既有呼叫行為不變。
- 未宣告 `executor`/`model_id` 的 specs 全部行為位元不變；既有測試（含 `tests/test_persona_phase4_fanout_autonomy.py`、`tests/test_deck_contract_alignment.py`、`tests/test_coordinator_manager_daemon.py`）全綠。
