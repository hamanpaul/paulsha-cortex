# generated-asset-attestation Specification

## Purpose

以不外洩憑證內容的方式，驗證 Manager／Monitor 相關生成資產與安裝後
runtime 的等價性，並補齊 trust-root 與 GitHub credential surface 的
attestation 覆蓋。

## Requirements
### Requirement: 依 canonical superpowers 規格驗收

本 change 的 canonical Requirements 載於 `docs/superpowers/specs/generated-asset-attestation-spec.md`；candidate MUST 滿足該規格的全部驗收條件，且 verify／review 以該規格為唯一需求來源。

#### Scenario: canonical 規格驗收

- **WHEN** 依 `docs/superpowers/specs/generated-asset-attestation-spec.md` 的 Requirements 對 candidate 驗收
- **THEN** 全部驗收條件成立
