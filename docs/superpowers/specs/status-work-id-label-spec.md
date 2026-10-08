---
status: accepted
work_item: status-work-id-label
---

# cortex inspect status 摘要行以 work_id 標示規格

## Requirements

對應 [#1367](https://github.com/hamanpaul/paulsha-cortex/issues/1367)。`cortex inspect status` 的文字模式，摘要行的方括號裡印的是 run_id 或 slice_id。operator 平常用 work_id，每次都要再去 `jobs.json` 對照。attention 條目其實已經帶有 `work_id`。

1. **R1 統一的標示 helper**：`paulsha_cortex/porcelain/inspect.py` 新增一個 helper，依序決定條目的標示：
   - 有 `work_id`、也有 `run_id`：`<work_id> (<run_id>)`
   - 只有 `work_id`：`<work_id>`
   - 沒有 `work_id`：沿用現行規則，先用 `run_id`，再用 `slice_id`，都沒有時用 `-`
2. **R2 四種摘要行都用它**：`needs_human[...]`、`quota_wait[...]`、`quota_decision[.../<persona>]`、`provider_failure[...]` 的方括號內容都改由 R1 的 helper 產生。`provider_failure` 現行只用 `slice_id`；改用 helper 之後，有 `work_id` 時改用 R1 的格式，沒有 `work_id` 時維持用 `slice_id`，避免改變無 work_id 條目的輸出。
3. **R3 `--json` 不變**：JSON 輸出的內容與位元組完全不變。
4. **R4 測試**：新增 `tests/test_status_work_id_label_1367.py`，先 RED 後 GREEN：
   - (a) 帶 `work_id` 與 `run_id` 的 attention 條目，四種摘要行都印 `<work_id> (<run_id>)`。
   - (b) 沒有 `work_id` 的條目，輸出與現行相同。
   - (c) `--json` 輸出不變。
   既有 `tests/test_diagnostic_invariant_family_527.py` 的斷言照舊成立；若既有測試的條目帶有 `work_id`，同步更新期望值，並在 terminal reason 說明。

## Boundary

- 只改 `paulsha_cortex/porcelain/inspect.py` 的文字輸出。不改 status 快照的產生端、不改 `--json`、不改其他子命令（`job`、`ready`、`work`）。
- spec／design／todo 的文字是 pinned authority，只能勾選 checkbox。

## Evidence

2026-10-06 到 10-08 的派工穩定性批次，兩個 operator session 監看 run 時都改成直接讀 `~/.agents/coordinator-cortex/jobs.json`，原因之一就是 status 文字模式只顯示 run_id，對不上 work_id。
