# agy-builder-support Specification

## Purpose
讓 agy 在 builder 語境產出可寫的 headless 形態（--mode accept-edits ＋ worktree-scoped --add-dir），planner／reviewer 維持 --mode plan --sandbox，write-forbidden builder 回到 plan+sandbox 並帶唯讀 --add-dir；--dangerously-skip-permissions 僅在 allow_unsafe 附加。
## Requirements
### Requirement: 依 canonical superpowers 規格驗收

本 change 的 canonical Requirements 載於 `docs/superpowers/specs/agy-builder-support-spec.md`；candidate MUST 滿足該規格的全部驗收條件，且 verify／review 以該規格為唯一需求來源。

#### Scenario: canonical 規格驗收

- **WHEN** 依 `docs/superpowers/specs/agy-builder-support-spec.md` 的 Requirements 對 candidate 驗收
- **THEN** 全部驗收條件成立
