# fix-agy-reviewer-json-schema Specification

## Purpose
讓 AGY reviewer 卡與 Claude reviewer 共用同一份 Manager terminal-contract schema（AGY 端以 Gemini 相容子集傳入 `--json-schema`），並讓 Manager 對 verification terminal 的 `details` 只接受物件或可正規化的非空字串，避免 reviewer 回傳形狀漂移造成整輪驗證失效（#880／#888）。
## Requirements
### Requirement: 依 canonical superpowers 規格驗收

本 change 的 canonical Requirements 載於 `docs/superpowers/specs/fix-agy-reviewer-json-schema-spec.md`；candidate MUST 滿足該規格的全部驗收條件，且 verify／review 以該規格為唯一需求來源。

#### Scenario: canonical 規格驗收

- **WHEN** 依 `docs/superpowers/specs/fix-agy-reviewer-json-schema-spec.md` 的 Requirements 對 candidate 驗收
- **THEN** 全部驗收條件成立

