# planning-artifact-manifest-binding Specification

## Purpose
本規格定義 planning publication 對 spec／design／plan 三類規劃產物的 work-item 綁定行為：即使 combo manifest 未宣告 brainstorming 輸出，規劃 runtime 仍可將三件套寫入 `docs/superpowers` 對應目錄；目的地必須是四段式、正規化的相對 Markdown 路徑，並依 kind 模板以 basename glob 包含該 work item，且保留 manifest 綁定與 OpenSpec 路徑的既有安全檢查。絕對路徑、`..`、跨目錄、非 `.md`、其他 work item 與 symlink 目的地必須拒絕；內容拒收時仍提供可操作的 needs_human 診斷提示。
## Requirements
### Requirement: 依 canonical superpowers 規格驗收

本 change 的 canonical Requirements 載於 `docs/superpowers/specs/planning-artifact-manifest-binding-spec.md`；candidate MUST 滿足該規格的全部驗收條件，且 verify／review 以該規格為唯一需求來源。

#### Scenario: canonical 規格驗收

- **WHEN** 依 `docs/superpowers/specs/planning-artifact-manifest-binding-spec.md` 的 Requirements 對 candidate 驗收
- **THEN** 全部驗收條件成立
