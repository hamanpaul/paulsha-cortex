# trust-root-home-fail-closed Specification

## Purpose
讓降權 job 的 HOME 像 PATH 一樣在 launch 前 fail-closed，避免 child process 回退到
unit/daemon HOME，並把 state 或 credentials 落到不可稽核的位置。
## Requirements
### Requirement: 依 canonical superpowers 規格驗收

本 change 的 canonical Requirements 載於 `docs/superpowers/specs/trust-root-home-fail-closed-spec.md`；candidate MUST 滿足該規格的全部驗收條件，且 verify／review 以該規格為唯一需求來源。

#### Scenario: canonical 規格驗收

- **WHEN** 依 `docs/superpowers/specs/trust-root-home-fail-closed-spec.md` 的 Requirements 對 candidate 驗收
- **THEN** 全部驗收條件成立
