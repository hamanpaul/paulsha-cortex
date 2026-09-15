# yaml-inline-list-quotes Specification

## Purpose
讓 zero-dependency YAML subset parser 以 quote-aware tokenizer 正確讀取 inline flow list，保留引號元素內的逗號、`]` 與跳脫引號。
## Requirements
### Requirement: 依 canonical superpowers 規格驗收

本 change 的 canonical Requirements 載於 `docs/superpowers/specs/yaml-inline-list-quotes-spec.md`；candidate MUST 滿足該規格的全部驗收條件，且 verify／review 以該規格為唯一需求來源。

#### Scenario: canonical 規格驗收

- **WHEN** 依 `docs/superpowers/specs/yaml-inline-list-quotes-spec.md` 的 Requirements 對 candidate 驗收
- **THEN** 全部驗收條件成立
