---
status: accepted
work_item: feat-slice-executor-model
---

# Tasks

- [ ] 1.1 RED：依 `docs/superpowers/plans/feat-slice-executor-model.md` 的 TDD RED 章節新增 `tests/test_slice_executor_model.py`，確認全部失敗。
- [ ] 1.2 實作至 GREEN，範圍限於 `docs/superpowers/specs/feat-slice-executor-model-spec.md` 的 Requirements（R1–R5）；未宣告 per-slice identity 的路徑行為位元不變，不動 #295 的 persona catalog 來源邏輯與 #205 的 workflow model chain override。
- [ ] 1.3 `changelog.d/feat-slice-executor-model.md` fragment 與 `CHANGELOG.md [Unreleased]` entry（#294）；CLI help、README、auto dispatch 契約文件同步。
- [ ] 1.4 `python3 -m pytest tests/ -q` 全綠；帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨。

## 驗收

單一 specs-dir 內異質 executor/model 的 slices 一次 fanout 各自以宣告 identity 派工（argv 與 job row 可稽核）；unknown identity 的 slice fail-closed 列可用清單、標 `needs_human` 且不波及同批；`depends_on` typo 在 held reasons 顯示 `deps-unknown:<id>`、跨 dir 合法依賴顯示 `deps-external:<id>`；fanout/tick 明確 `(executor, model)` 對 unknown 時拒絕；未宣告與不帶 model 的既有呼叫行為位元不變。
