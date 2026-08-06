---
status: accepted
work_item: fix-persona-catalog-portability-v2
---

## Goals

修正 verification gate 從被治理 repo 的 git tree 讀 cortex 套件自身 persona catalog 的可攜性缺陷（#295 primary、#291 duplicate）：canonical 來源改為 cortex 套件內建 catalog，被治理 repo 可選擇性提供 repo-local override，讓非 cortex repo 的 slice 能通過 persona gate。

## Why

`run_result_verification` 無條件 `git show {dispatch_base}:paulsha_cortex/persona/personas.yaml` 讀**目標 repo**，該檔只存在於 paulsha-cortex 自身，因此任何被治理的非 cortex repo 都確定性停在 `needs_human(persona-catalog-unreadable)`；同時 `dispatch: auto` 又強制要求恰好一個 persona-scope check，構成「必須宣告、宣告必失敗、拿掉則 spec 不合法」的封閉矛盾。IntelliDbgKit（#295）與 serialwrap（#291）皆實測 build 成功但產出被卡在 verification 門口，下游 `depends_on` 永不滿足。

## What Changes

- persona catalog 的 canonical 來源改為 cortex 套件內建（`paulsha_cortex.persona.loader.DEFAULT_PERSONAS_PATH`），復用既有 `_load_catalog_from_text` 驗證。
- 以 `git cat-file -e` 探測 `dispatch_base` tree 是否存在 repo-local override：存在即優先並 pin 在 `dispatch_base` commit 讀取；不存在即回退 packaged catalog。
- override 宣告存在但不可讀維持 `persona-catalog-unreadable`、不合法維持 `persona-catalog-invalid`，不靜默回退 packaged；packaged 自身壞損亦 fail-closed。
- cortex repo 自身因 git tree 內有 catalog 而自然落在 override 分支，行為不退化，不做 repo 身分特判。
- evidence 的 `persona_catalog` 增加來源標記（`repo-local`／`packaged`）；讀取失敗的錯誤帶實際嘗試過的來源路徑。
- `dispatch: auto` 的 persona-scope 必要性檢查維持不變（packaged fallback 已解除封閉矛盾）。

## Capabilities

### Modified Capabilities

- `trusted-dispatch-completion`：deterministic ResultVerification 的 persona catalog 來源解析。詳見 `docs/superpowers/specs/fix-persona-catalog-portability-v2-spec.md` 的 Requirements 與 `docs/superpowers/specs/fix-persona-catalog-portability-v2-design.md` 的 Decisions。
