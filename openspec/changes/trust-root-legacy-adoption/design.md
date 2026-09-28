# Trust root legacy adoption 設計

## Context

參考主機（9900X）的 system trust-root 是 Phase 2b 手工部署：`/opt/cortex/venv` 為 0.1.8 實體目錄、沒有 `/var/lib/cortex-installer`、五個 cortex 帳號的 uid 為 999／997／995／994／993，`/var/lib/cortex` 約 21 GB 並帶 ACL，`/etc/systemd/system` 有 9 個 cortex unit 與一個 installer 未產生的 reviewer drop-in。installer 現行模型：

- `build_install_plan` 把 config 轉成 canonical plan；config schema 已接受任意 uid／gid，`legacy_policy` 只驗值並寫進 plan（`core.py` 的 config key 集合與 `build_install_plan`）。
- `validate_preflight` 對既有帳號要求 uid／gid／home／shell 完全相符**且**有本次 receipt 或 prior receipt provenance；uid／gid 被別的身分持有直接 `AccountCollisionError`。
- 既有目錄與資產只接受兩種證明：本次 receipt 的 journal，或 `--prior-receipt` 的逐 step provenance（`_prior_step_provenance`、`adopted_from_receipt`、mount adoption）。
- 既有目錄的 metadata replacement 已有非遞迴、fd-based、先 checkpoint dev／ino 的 `replacing` 路徑，可以 replay 與 rollback。

以參考主機的 uid 產生 plan，26 個 generated artifact 與標準 uid 版本逐位元組相同，只有五個 `account:*` step 不同；所以保留舊 uid 不需要對 state 做任何 chown。

## Goals / Non-Goals

**Goals**

- 讓沒有 receipt 的舊主機能在 operator 明確審核下被 installer 接手，接手後的 receipt 可作為日後升級的 `--prior-receipt`。
- 全程 fail-closed：inventory、plan、apply 之間任何漂移都在第一個 mutation 前停下；apply 中途的失敗由 durable receipt 提供 rollback 權限。
- 不刪除、不覆寫任何 legacy 物件；rollback 能證明主機回到接手前的狀態。
- 沒有 legacy block 的 plan 行為與現行完全相同。

**Non-Goals**

- 不 remap uid／gid，不修改非 cortex 系統身分。
- 不遞迴重寫既有檔案的 owner 或 ACL。
- 不遷移 quarantine 內的舊 state 內容到新部署（operator 需要時另行人工處理）。
- 不修 repository step 拒收執行期 branch／worktree 的問題（#1124）；本設計把 repo 移入 quarantine 後重新 clone。

## Decisions

### D1 證明鏈：inventory digest 綁定 plan，apply 時重新擷取

root 擷取 inventory（唯讀，service 可繼續運轉）並以 no-overwrite 方式發布到 `/var/lib/cortex-installer/legacy/<sha>.json`。plan 綁定 inventory 的穩定欄位 digest、scope digest 與 host binding；operator 沿用 runbook 的三方確認流程確認 plan sha。apply 在取得 maintenance lease、停服務之後重新擷取，三者必須相等。`active_state`、擷取時間、in-flight、census 計數等不穩定欄位不進 digest，apply 時重新計算並當作 gate。

**替代方案**：直接信任 plan 階段的觀察。否決：plan 與 apply 之間可能相隔數小時，service 仍在運轉。

### D2 host overlay 承載主機差異

`--host-overlay` 只允許 allowlist 欄位，plan 記錄 overlay digest；overlay 持久存放於 `/var/lib/cortex-installer/host-overlay.yaml`（root 0600），之後每次升級沿用，才能產生相同的 `account:*` step 讓 prior-receipt 交接成立。

**替代方案**：operator 直接改寫 release 的 install config。否決：release 封存的 config 失去權威，差異不可審。

### D3 disposition 規則與「必要子集」

| 物件 | disposition |
|---|---|
| 帳號完全符合 overlay | `adopt`（無 mutation） |
| 帳號任一欄位不符，或 uid／gid 由非 cortex 身分持有 | plan 失敗 |
| plan 受管的目錄、型別正確 | `adopt-in-place`：相符不動；漂移做非遞迴 metadata replacement |
| plan 受管路徑型別錯誤 | plan 失敗 |
| 受管 generated 檔、不符的受管 symlink | `quarantine-then-create` |
| 完全相符的受管 symlink | `adopt` |
| `toolchain/bin` 整個目錄 | `quarantine-then-create`（一次 rename 同時避開大檔與 symlink） |
| venv active link 位置是實體目錄 | `quarantine`，再由 venv step 建立 symlink |
| venv active link 是沒有 receipt 的 symlink | plan 失敗 |
| source repository | `quarantine-then-clone` |
| job worktree pool | `quarantine-then-create` |
| state 根目錄下 plan 未宣告的頂層項目 | `quarantine` |
| 受管目錄內未列管的子目錄、`tmp*.tmp`、`*.rollback.bak` 等殘留 | `quarantine` |
| installer 未產生的權威面物件（unit drop-in、`.env.bak-*` 等 verify 會判 FAIL 的類別） | `quarantine` |
| credential 類物件 | `quarantine`，再依 plan 的 `required_credentials` 重新匯入 |
| deploy root 內非權威的舊備份（venv 備份、operator-backups、`toolchain/lib/*`） | `quarantine` |
| census 中 job 帳號可寫、但不在宣告資產或 `census_exceptions` 的路徑 | plan 失敗 |
| 其他 | `unclassified` → plan 失敗 |

「必要子集」依 owner 裁決：只有 plan 宣告受管的 state 目錄連同內容就地接手（coordinator、monitor、control、registry、config、run、runtime 下的受管路徑）；其餘一律移入 quarantine。舊 run 若引用被 quarantine 的 worktree 或 manifest，接手後由 operator 以 abandon／retire 收尾。

### D4 `legacy-quarantine` step

`{step_id: "legacy-quarantine:<path>", kind: "legacy-quarantine", path, destination, expected: {type, uid, gid, mode, dev, ino, sha256|link_target}, row_sha256, operations: ["snapshot","rename-noreplace"], rollback_policy: "restore"}`。一般檔綁定 `sha256`、symlink 綁定 `link_target`；目錄只綁定身分（type／owner／mode／dev／ino），不綁定樹內容——被 quarantine 的內容從不被接手或採信，接手後一律重建，rollback 以 dev／ino 證明搬回的是同一個物件；對數十萬子孫的 worktree pool 計算樹 digest 既不可行也無法在服務運轉時穩定。destination 為 `<quarantine_root>/<inventory_sha 前 16 碼>/root/<原絕對路徑>`。quarantine step 排在同路徑或其下任何 step 之前、在其父目錄受管 step 之後。拓撲驗證與 apply plan schema 驗證要認得此 kind；destination 父目錄鏈由 step 自行建立（root 0700），不列為受管路徑。

**替代方案**：copy 後刪除。否決：跨檔案系統複製 21 GB 不可行，且刪除違反「不刪除 legacy 物件」原則。rename 要求同檔案系統；參考主機的 `/`、`/opt`、`/etc`、`/var/lib` 同在一個 ext4。

### D5 provenance 與 receipt

receipt 頂層新增 `legacy_adoption`（inventory digest、inventory 路徑、host binding、quarantine root、apply 時 inventory digest）。接手的 entry 帶 `adoption: {source: "legacy-inventory", row_sha256}`；quarantine entry 帶 `quarantine_authority`（source dev／ino、destination）。provenance helper 收斂成單一 `_entry_has_adoption_provenance(entry, plan)`，接受 `prior.exists is False`、`adopted_from_receipt is True`，或 legacy adoption row 與 plan 綁定值相符。**不得**以 `adopted_from_receipt=true` 冒充，稽核上必須分得出來源。

### D6 ACL 不遞迴，以 census 把關

只修正受管目錄 inode 本身的 metadata；既有檔案保留舊 ACL（default ACL 只影響新建檔）。安全性改由 census 保證：root 列舉 state、deploy、systemd、polkit 內的路徑，各 job 帳號以 `access(2)` 由 kernel 判定可寫性（排除 symlink），可寫路徑必須落在 plan 宣告的可寫資產或 `census_exceptions` 內。參考主機實測只有未列管的 `coordinator/review-sandboxes` 一處例外，依 D3 會被移入 quarantine。

### D7 rollback 證明

rollback 反向處理 journal（刪除新建物件、還原目錄 metadata、以 `RENAME_NOREPLACE` 搬回 quarantine 物件），有任何 systemd 路徑變動時最後執行 `daemon-reload`，再以 plan scope 重新擷取 inventory；穩定欄位 digest 等於原值才回報 `legacy_restored=true` 並納入 `restore_safe`。`list_unknown_state` 要把 quarantine source 視為受管路徑，避免搬回後被誤判為 unknown。

### D8 RC qualification

新增 `legacy-adoption` profile：以 fixture manifest 在 disposable container 內布置 Phase 2b 形狀的舊主機（非標準 uid、實體 venv、手工 unit 與 drop-in、帶 ACL 的 state、未列管目錄、worktree pool、credential 檔），再跑 inventory→plan→apply→activate→verify→rollback，並檢查 rollback 後 inventory digest 相等。release 含 `paulsha_cortex/trust_root/install/` 或 `qualification/` 變更時，release preflight 要求 exact SHA 上有通過的 `legacy-adoption` run。

## Risks / Trade-offs

- **舊檔 ACL 不重寫** → 舊檔可能保留比 plan 寬的讀取權；由 census 與既有 R9 attack matrix 驗證寫入面，讀取面接受現狀並在 inventory 摘要揭露。
- **只接手必要子集** → 舊 run 的 evidence 與 worktree 離線；quarantine 保留可人工取回，舊 run 以 abandon／retire 收尾。
- **inventory 規模** → state 不遞迴 hash、不列子孫，只記受管路徑與摘要；前置 #1123 讓 receipt 的目錄快照與子孫數脫鉤。
- **rename 依賴同檔案系統** → 跨檔案系統時 fail-closed，由 operator 另行調整 quarantine root。
- **#568 reviewer drop-in** → #1125 實測證明現行 reviewer argv（`--add-dir <sandbox>`）搭配 RC pin 的 agy 1.2.11 不需要 bounded settings；舊 drop-in 與其 json 標為「已由 launcher 取代」後移入 quarantine，不移植寫死的 sandbox hash。前提是 adoption 同時換上新程式碼與 RC pin 的 toolchain；只拔 drop-in 而仍跑舊 argv 加舊 agy 會退回讀檔被拒的問題。

## Migration Plan

1. #1123 落地（#1125 已查證不需要程式變更）。
2. 本 change 的程式 PR 依序落地（inventory → plan／overlay／disposition → preflight／apply／rollback → RC profile → runbook），release 通過 `legacy-adoption` profile。
3. operator 在參考主機：凍結 user-level instance 與 timer → `legacy inventory` → 審核 `legacy show` → 以持久化 overlay 產生 plan → 三方確認 → lease、停服務 → `apply --legacy-inventory` → credential 重新匯入 → activate → verify。
4. 失敗時依 runbook rollback；`legacy_restored=true` 後恢復原本 active 的 service（舊版）。

## Open Questions

- legacy env 覆寫與舊 copilot OAuth 設定（`PSC_COPILOT_OAUTH_CONFIG`）轉成受支援形態的方式，於 inventory PR 盤點後定案。
- reviewer HOME 內不在 providers 清單的 codex 登入檔：依 D3 視為 credential 類移入 quarantine，不重新匯入。
