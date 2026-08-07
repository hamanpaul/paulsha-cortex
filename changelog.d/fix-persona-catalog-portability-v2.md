### Fixed
- **Issue #295（primary）／#291（duplicate）：persona catalog 改以套件內建為 canonical
  來源，非 cortex repo 的 slice 不再確定性卡 `persona-catalog-unreadable`**：
  `run_result_verification` 原本無條件 `git show {dispatch_base}:paulsha_cortex/persona/personas.yaml`
  讀**目標 repo**，該檔只存在於 paulsha-cortex 自身，任何被治理的非 cortex repo
  builder Job 即使成功產出，verification 也必然 `needs_human` / `persona-catalog-unreadable`，
  且 `dispatch: auto` 又強制要求恰好一個 `persona-scope` check，無法拿掉繞開。
  改為先以 `git cat-file -e {dispatch_base}:paulsha_cortex/persona/personas.yaml`
  探測被治理 repo 的 `dispatch_base` tree 是否宣告 repo-local override：
  存在即維持既有行為（pin 在 `dispatch_base` commit 讀取、fail-closed）；不存在則
  回退讀取 `paulsha_cortex.persona.loader.DEFAULT_PERSONAS_PATH` 的套件內建
  catalog，一樣經 `_load_catalog_from_text` schema 驗證後完成 persona-scope 判定。
  override 宣告存在但讀取失敗／schema 不合法時，仍分別維持
  `persona-catalog-unreadable`／`persona-catalog-invalid`，不靜默回退 packaged catalog；
  packaged catalog 自身壞損（安裝損壞）亦同樣 fail-closed。cortex repo 自身因
  git tree 內本就有 catalog 而自然落在 override 分支，行為不退化。evidence 的
  `persona_catalog` 新增 `source`（`repo-local`／`packaged`）欄位可稽核判定依據；
  讀取失敗的錯誤 payload 帶實際嘗試過的來源路徑，operator 可分辨是設定問題還是
  真違規。`dispatch: auto` 對恰好一個 `persona-scope` check 的要求維持不變。
