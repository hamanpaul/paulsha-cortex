# fix-monitor-fs-event-convoy Specification

## Purpose
以單一 refresh worker 與 dirty 合併取代每個 filesystem event 一條 refresh 執行緒，消除 monitor event convoy 與資源無界累積。
## Requirements
### Requirement: 依 canonical superpowers 規格驗收

本 change 的 canonical Requirements 載於 `docs/superpowers/specs/fix-monitor-fs-event-convoy-spec.md`；candidate MUST 滿足該規格的全部驗收條件，且 verify／review 以該規格為唯一需求來源。

#### Scenario: canonical 規格驗收

- **WHEN** 依 `docs/superpowers/specs/fix-monitor-fs-event-convoy-spec.md` 的 Requirements 對 candidate 驗收
- **THEN** 全部驗收條件成立
