---
status: accepted
work_item: agy-builder-support
---

# Agy Builder Support Specification

## Requirements

- Builder 語境（`SubprocessLauncher` 的 `read_only=False` 且 `review_only=False`）下，agy 的 launcher MUST
  產出可寫的 headless 形態：`agy --print <prompt> --mode accept-edits --add-dir <worktree> --model <id>`；
  planner（`read_only`）與 reviewer（`review_only`）形態 MUST 維持現行 `--mode plan --sandbox` 不變。
- `--dangerously-skip-permissions` MUST 只在 `allow_unsafe=True` 時附加；預設 builder 形態不附。
- agy builder 的 commit 契約 MUST 與 codex／copilot／claude 對齊（`commit_required` sentinel／branch commits），
  使 Manager 能以既有 harvest 路徑取得 candidate。
- registry 宣告 `executor: agy` 具 `build` capability 時，launcher MUST 能產出可寫形態；此對應 MUST 由測試機械釘住，
  不得只靠註解。
- 不改動其他 executor 的權限剖面、不改 model-identities schema、不改 release policy。

## Acceptance

- `tests/test_coordinator_agy_launcher.py` 新增：builder 形態斷言（含 `--mode accept-edits`、`--add-dir <worktree>`、無 `--sandbox`）、
  `allow_unsafe` 分支斷言、planner／reviewer 形態回歸斷言；`SubprocessLauncher(executor="agy")` 在 builder 語境不再拋
  `agy executor refuses unsafe mode`。
- focused／full repository gates 綠；candidate evidence 記入 Cortex；PR 以 `Closes #799` 交付。
- live agy build session 的 headless smoke（#568 的 jetski 權限剖面）為 operator **post-merge** 部署後驗收，
  不在 candidate 階段要求 live model session。
