---
status: accepted
work_item: fix-persona-catalog-portability-v2
---

## ADDED Requirements

### Requirement: persona catalog 來源解析必須可攜且 fail-closed

ResultVerification 的 persona gate MUST 以 cortex 套件內建 catalog（package-relative `DEFAULT_PERSONAS_PATH`）為 canonical 來源。被治理 repo 的 `dispatch_base` tree 內存在 `paulsha_cortex/persona/personas.yaml` 時，MUST 以該 repo-local override 優先，並 pin 在 `dispatch_base` commit 讀取；不存在時 MUST 回退 packaged catalog 完成 scope 判定，MUST NOT 因此進入 `needs_human`。

override 宣告存在但不可讀 MUST 維持 `persona-catalog-unreadable`；可讀但解析或 schema 不合法 MUST 維持 `persona-catalog-invalid`；兩者皆 MUST NOT 靜默回退 packaged catalog。packaged catalog 自身不可讀或不合法亦 MUST fail-closed。

evidence 的 `persona_catalog` MUST 記錄採用來源標記（`repo-local`／`packaged`）、來源路徑與 content hash；catalog 讀取失敗的錯誤 payload MUST 帶實際嘗試過的來源路徑。

#### Scenario: 非 cortex repo 無 repo-local catalog

- **WHEN** builder Job 於 `dispatch_base` tree 不含 `paulsha_cortex/persona/personas.yaml` 的被治理 repo exit 0 進入 verification
- **THEN** persona gate 以 packaged catalog 完成 scope 判定，不產生 `persona-catalog-unreadable`
- **THEN** evidence 的 `persona_catalog` 記錄 `source: packaged` 與 content hash

#### Scenario: repo-local override 優先且 pin 在 dispatch_base

- **WHEN** `dispatch_base` tree 內存在該路徑（cortex repo 自身即此情境）
- **THEN** persona gate 以該 commit 的 catalog 內容做 scope 判定並記錄 content hash
- **THEN** 行為與既有 cortex repo verification 一致，不退化

#### Scenario: override 宣告存在但壞損

- **WHEN** 該路徑存在於 `dispatch_base` tree 但讀取失敗，或可讀但 schema 不合法
- **THEN** slice 進入 `needs_human`，reason 分別為 `persona-catalog-unreadable`／`persona-catalog-invalid`
- **THEN** 錯誤 payload 帶實際嘗試過的來源路徑，不靜默回退 packaged catalog
