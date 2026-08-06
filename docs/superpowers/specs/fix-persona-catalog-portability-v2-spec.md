---
status: accepted
work_item: fix-persona-catalog-portability-v2
---

# fix-persona-catalog-portability Specification

#295（primary）＋#291（duplicate）：verification gate 在被治理 repo 內 `git show paulsha_cortex/persona/personas.yaml`，非 cortex repo 必卡 `needs_human(persona-catalog-unreadable)`。改為以 cortex 套件內建 catalog 為 canonical 來源、被治理 repo 可選擇性提供 repo-local override，讓跨 repo 治理的 slice 能通過 persona gate。

## 背景

`run_result_verification` 在 persona scope 判定前，無條件從**目標 repo** 的 `dispatch_base` commit 讀取 `paulsha_cortex/persona/personas.yaml`（`paulsha_cortex/coordinator/verification.py:27` 的 `PERSONA_CATALOG_PATH` 常數、`:780-804` 的 `git show` 段）。該檔只存在於 paulsha-cortex 自己的 git tree，因此任何被治理的非 cortex repo，builder Job 即使成功產出，verification 也確定性停在 `needs_human` / `persona-catalog-unreadable`。

同時 `dispatch: auto` 強制要求恰好一個 persona-scope check（`verification.py:284-294`），spec 拿掉該 check 即 `invalid-frontmatter`。兩邊構成封閉矛盾：必須宣告，宣告必失敗，拿掉則 spec 不合法。#295 於 IntelliDbgKit、#291 於 serialwrap 皆實測到 build 成功但產出被結構性卡在 verification 門口，下游 `depends_on` 永不滿足。

`paulsha_cortex/persona/loader.py:12` 已有 package-relative 的 `DEFAULT_PERSONAS_PATH` 可復用：persona 定義是 cortex 的產品資產，隨安裝套件發佈，不是被派工 repo 的資產。

## Goals

- 非 cortex repo（無 repo-local catalog）的 slice 可通過 persona gate，跨 repo 治理不再於 verify 階段確定性失敗。
- 被治理 repo 可用 repo-local catalog override 覆寫 packaged catalog，且壞損時維持 fail-closed。
- cortex repo 自身（repo 內有 catalog）行為不退化。
- catalog 讀取失敗時 operator 可從錯誤訊息分辨是設定問題還是真違規。

## Requirements

### R1 packaged catalog 為 canonical 來源

verification 的 persona gate SHALL 以 cortex 套件內建 catalog（`paulsha_cortex.persona.loader.DEFAULT_PERSONAS_PATH`，package-relative）為 canonical 來源。

被治理 repo 的 `dispatch_base` tree 內不存在 `paulsha_cortex/persona/personas.yaml` 時，persona gate MUST 以 packaged catalog 完成 scope 判定，MUST NOT 回 `needs_human(persona-catalog-unreadable)`。packaged catalog 內容 MUST 經與現行 `_load_catalog_from_text` 相同的 schema 驗證。

### R2 repo-local override 以 dispatch_base tree 存在性判定

被治理 repo MAY 於自身 git tree 提供同路徑檔案 `paulsha_cortex/persona/personas.yaml` 作為 repo-local override。

override 生效判定 MUST pin 在 `dispatch_base` commit：該 commit tree 內存在該路徑即 override 生效並優先於 packaged catalog；不存在即回退 packaged catalog。override 生效時 MUST 從 `dispatch_base` commit 讀取內容（不讀 working tree），維持與現行相同的 pinning 語意。

### R3 override 宣告存在但壞損 MUST fail-closed

override 於 `dispatch_base` tree 宣告存在但內容不可讀時，MUST 維持 `needs_human` / `persona-catalog-unreadable`；可讀但解析或 schema 不合法時 MUST 維持 `needs_human` / `persona-catalog-invalid`。兩者皆 MUST NOT 靜默回退 packaged catalog。

packaged catalog 自身不可讀或不合法（安裝損壞）時亦 MUST fail-closed，分別對應 `persona-catalog-unreadable` 與 `persona-catalog-invalid`。

### R4 cortex repo 自身行為不可退化

repo git tree 內有 catalog 的 repo（含 cortex repo 自身）MUST 落在 R2 的 override 分支：仍自 `dispatch_base` commit 讀取 catalog、以其內容做 scope 判定並記錄 content hash，與現行為一致。既有 verification 測試 MUST 在不改斷言語意的前提下維持全綠。

### R5 錯誤與 evidence 必須可稽核來源

persona catalog 讀取失敗時，錯誤 payload MUST 帶實際嘗試過的來源：repo-local 分支記 repo 內路徑與 `dispatch_base` commit；packaged 分支記 packaged 來源路徑。

成功時 evidence 的 `persona_catalog` MUST 記錄採用來源標記（`repo-local` 或 `packaged`）、來源路徑與 content hash，使事後可稽核 scope 判定依據哪一份 catalog。

## 非目標

- 不放寬 `dispatch: auto` 對恰好一個 persona-scope check 的要求（`verification.py:284-294`）；packaged fallback 已解除「必須宣告、宣告必失敗」的封閉矛盾，該檢查維持原樣。
- 不新增 env／instance config 指定 catalog 路徑的通道（#295 期望選項 2）；本次採選項 1（packaged canonical）加上 repo-local override。
- 不改 persona scope 判定演算法（`gate.build_verdict`）與 catalog schema 驗證規則。
- 不處理 #291 附帶觀察的 manager 單例、systemd sanitized env、target branch 預先存在、builder Bash 白名單議題（各屬另案）。

## 驗收面

- 非 cortex repo（無 repo-local catalog）的 slice verification 通過 persona gate，不再 `needs_human(persona-catalog-unreadable)`。
- repo-local override 存在時優先於 packaged，且內容 pin 在 `dispatch_base` commit。
- override 宣告存在但不可讀／不合法時 fail-closed，reason 分別維持 `persona-catalog-unreadable`／`persona-catalog-invalid`。
- cortex repo 自身現行為不變，既有 verification 測試全綠。
- evidence 記錄 catalog 來源標記與 hash；讀取失敗的錯誤帶實際嘗試過的來源路徑。
