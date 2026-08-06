---
status: accepted
work_item: fix-dispatch-spec-path
---

## Goals

修正 `autonomy._infer_repo_root()` 在 spec 位於 repo 外（如 `~/.agents/specs`）時未回退至 `PSC_REPO_ROOT`，導致 repo-relative plan 路徑解析到錯誤 root，造成 dispatch pinning 失敗。

## Why

`#98` 回報：當 spec 檔位於 repo 外（`~/.agents/specs/<slug>-spec.md`），`_infer_repo_root` 沿 parent 目錄尋找 `.git` 或直接回傳 `spec_path.parent`，而非使用已設定的 `PSC_REPO_ROOT`。結果：repo-relative `plan` 路徑被解析到錯誤 root → `plan file unreadable for dispatch pinning` → `dispatch_ready` 判定失敗 → 整個 dispatch 鏈中斷。這在 dogfood 派工流程中是高頻路徑。

## What Changes

- `paulsha_cortex/coordinator/autonomy.py`：`_infer_repo_root()` 當 spec 不在 repo root 子目錄下時，回傳 `paths.repo_root()`（即 `PSC_REPO_ROOT`），因為 repo-relative contract path 應以設定 repo root 為基準。

## Capabilities

### Modified Capabilities

- `coordinator-autonomy`：spec 在 repo 外時 `_infer_repo_root` 回退至 `PSC_REPO_ROOT`，確保 repo-relative plan 路徑正確解析。