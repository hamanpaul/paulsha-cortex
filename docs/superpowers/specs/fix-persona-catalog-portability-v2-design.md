---
status: accepted
work_item: fix-persona-catalog-portability-v2
---

# fix-persona-catalog-portability Design

## Decisions

### D1 canonical catalog 改為套件內建來源，復用既有驗證路徑

verification 讀 catalog 的 canonical 來源改為 `paulsha_cortex.persona.loader.DEFAULT_PERSONAS_PATH`（`persona/loader.py:12`，package-relative，隨安裝發佈）。packaged 檔案文字讀入後仍走既有 `_load_catalog_from_text`（`verification.py:478`）做 schema 驗證，不另開驗證路徑。

理由：根因是 verification 隱含假設「被治理 repo == cortex repo」。persona 定義是 cortex 的產品資產，該從安裝的套件解析，而不是要求每個被治理 repo 的 git tree 都長出 cortex 的內部檔案。復用 `_load_catalog_from_text` 讓 packaged 與 repo-local 兩個來源吃同一套驗證，行為可預測。

### D2 override 偵測與讀取分離：以 dispatch_base tree 存在性探測

先以 `git cat-file -e {dispatch_base}:{PERSONA_CATALOG_PATH}` 探測 override 是否宣告存在：exit 0 表示宣告存在，必須接著以既有 `git show` 讀取成功，讀不到即 fail-closed；exit 非零視為未宣告，回退 packaged catalog。

理由：單靠 `git show` 失敗無法區分「路徑不存在於該 commit」與「repo 或物件層的其他錯誤」，解析 stderr 字串脆弱且受 locale 影響。existence probe 讓「未宣告，回退」與「宣告但不可讀，fail-closed」成為兩個確定性分支，正好對應架構裁決的兩個語意。

風險與緩解：repo 整體損壞時 `cat-file -e` 也回非零，會被歸類為「未宣告」——但緊接的 persona scope diff（`verification.py:805-819`）仍要對同一 repo 跑 `git diff`，repo 損壞會在該步以 `persona-scope-error` fail-closed，不會靜默通過。

### D3 override 壞損不回退，fail-closed 語意維持

override 宣告存在但讀取失敗維持 `persona-catalog-unreadable`；可讀但解析／schema 不合法維持 `persona-catalog-invalid`。不因 packaged catalog 可用而靜默降級。packaged 分支自身讀檔 OSError 對應 `persona-catalog-unreadable`、驗證 ValueError 對應 `persona-catalog-invalid`。

理由：repo 明示提供 override，代表其 persona 契約可能刻意與 packaged 不同；壞損時退 packaged 等於拿錯的契約做 scope 判定，且 operator 事後看不出來。靜默回退是最糟的行為，fail-closed 加明確 reason 才是 verification 既有的一貫語意。

### D4 cortex repo 行為由 override 分支自然保留，不做 repo 身分特判

cortex repo 自身 git tree 本來就有 `paulsha_cortex/persona/personas.yaml`，必然落在 override 分支：仍 pin 在 `dispatch_base` commit 讀取、記 content hash，與現行為一致。程式不辨識「這個 repo 是不是 cortex」。

理由：以「檔案是否存在於 dispatch_base tree」做行為分界是可測、確定性的；以 repo 身分特判需要辨識 remote／路徑等環境訊號，脆弱且在 fork、鏡像、worktree 情境下語意不明。

### D5 evidence 加來源標記、錯誤帶 attempted sources（additive）

成功時 `details["persona_catalog"]` 增加 `"source"` 欄位（`"repo-local"` 或 `"packaged"`）；repo-local 分支既有欄位（`path`／`commit`／`hash`）不變；packaged 分支 `path` 記 packaged 來源路徑、`commit` 記 `None`、`hash` 記內容 sha256。失敗時 error payload 記錄實際嘗試過的來源（repo-local 的 path 加 commit，或 packaged 的 path）。

理由：#295 明確要求 operator 能分辨「catalog 不可得」是設定問題還是真違規；來源標記讓每次 scope 判定的依據可稽核。additive 欄位不動 repo-local 分支既有形態，evidence consumer 不受影響；packaged 分支的新形態只出現在過去必 fail 的情境，沒有回溯相容問題。

## 風險與緩解

- **既有測試的 FakeGitRunner 對未知 git call 直接 AssertionError**：新增 `cat-file -e` 探測呼叫後，`tests/test_coordinator_candidate_verification.py` 既有走到 catalog 段的測試需在 response map 補探測項（回 exit 0）。屬機械性同步，plan 以獨立 task 一次到位，驗收為該測試檔全綠。
- **packaged catalog 與被治理 repo CI 端 persona-scope 檢查的 catalog 版本漂移**：verification evidence 記 source 與 content hash，漂移可稽核；catalog 內容治理仍歸 cortex repo，被治理 repo 要客製即走 repo-local override 顯式宣告。
- **packaged catalog 在安裝損壞時缺檔**：D3 已涵蓋——packaged 讀取失敗同樣 fail-closed 並在錯誤中帶 packaged 路徑，operator 可直接看出是安裝問題而非 repo 問題。
