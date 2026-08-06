---
status: accepted
work_item: fix-persona-catalog-portability-v2
---

# fix-persona-catalog-portability Design

## Decisions

- persona catalog 的 canonical 來源改為 cortex 套件內建 catalog（`persona/loader` 的
  package-relative `DEFAULT_PERSONAS_PATH`）；persona 定義是 cortex 的產品資產，隨安裝
  套件發佈，不是被派工 repo 的資產。
- repo-local override 以 `git cat-file -e <dispatch_base>:<catalog path>` 在 `dispatch_base`
  commit tree 判定存在性：存在即必須讀取成功，否則維持 fail-closed
  （`persona-catalog-unreadable`／`persona-catalog-invalid` 分流）；不存在即回退 packaged
  catalog，兩者為確定性分支，不解析 stderr 字串。
- cortex repo 自身（git tree 內有 catalog）落在 override 分支，行為與現行一致，不做
  repo 身分特判。
- 兩來源共用既有 `_load_catalog_from_text` schema 驗證，不另開驗證路徑。
- evidence 的 `persona_catalog` 增記來源標記（`repo-local`／`packaged`）與 content hash；
  讀取失敗錯誤帶實際嘗試過的來源路徑。
- `dispatch: auto` 對恰好一個 persona-scope check 的要求維持原樣（packaged fallback 已
  解除「必須宣告、宣告必失敗」的封閉矛盾）。

詳細 D1–D5 與風險緩解見 `docs/superpowers/specs/fix-persona-catalog-portability-v2-design.md`。
