---
status: executable
work_item: trust-root-legacy-adoption
audience: operator
authority: transactional-installer
refs:
  - openspec/changes/trust-root-legacy-adoption/specs/trust-root-legacy-adoption/spec.md
  - docs/superpowers/runbooks/trust-root-transactional-install.md
  - paulsha_cortex/trust_root/install/legacy.py
  - qualification/legacy_adoption.py
---

# Trust Root legacy adoption

本 runbook 用於**沒有 transactional installer receipt** 的舊主機（Phase 2b 手工部署），讓 installer 在 operator 明確審核下接手。已有 applied＋qualified receipt 的主機一律走 `trust-root-transactional-install.md` 的 `--prior-receipt` 升級，不適用本文。

規格：`openspec/changes/trust-root-legacy-adoption`（#1122）。RC qualification 的 `legacy-adoption` profile（`qualification/legacy_adoption.py`）在 disposable container 內以相同步驟實跑；本文的步驟順序與旗標以該 harness 為準。

## 0. 前提

- 要安裝的 release 已通過 exact SHA 的 RC `legacy-adoption` profile（release 含 `paulsha_cortex/trust_root/install/` 或 `qualification/` 變更時，release preflight 會強制要求）。
- 依 `trust-root-transactional-install.md` §1 封存 candidate CLI（`cortex_root_cli`），本文所有 root 指令都經由它執行。
- quarantine root（預設 `/var/lib/cortex-installer/legacy-quarantine`）與所有會被 quarantine 的物件在**同一個檔案系統**：搬移只用 `renameat2(RENAME_NOREPLACE)`，跨檔案系統會 fail closed，沒有 copy fallback。
- 主機提供 `/proc`（rollback 以 `/proc/self/fdinfo` 判定掛載點），且 `getfacl`／`setfacl` 可用。
- system instance 沒有在飛 job；本流程會停止 `cortex-egress-proxy`、`cortex-manager`、`cortex-monitor` 三個 service。

## 1. 保留原則

- **不刪除、不覆寫任何 legacy 物件。** installer 未產生、會衝突或 plan 未宣告的物件一律 rename 進 quarantine；受管目錄只修改 inode 本身的 metadata。
- **state 只接手必要子集**（owner 裁決）：plan 宣告受管的 state 目錄連同內容就地接手；state 根目錄下 plan 未宣告的頂層項目、受管目錄內未列管的子目錄與 `tmp*`／`*.rollback.bak` 殘留、job worktree pool、source repo、credential 類物件一律 quarantine。
- **不 remap uid／gid**：帳號以 host overlay 宣告現有 id；uid／gid 被非 cortex 身分持有時 plan 失敗。
- 既有檔案的 ACL 不遞迴重寫；安全性由 writable census 把關（各 job 帳號以 `access(2)` 判定可寫路徑）。

## 2. host overlay

overlay 只允許以下鍵，其他任何鍵都會讓 plan 失敗：

```yaml
accounts:
  cortex-manager: {uid: <現有 uid>, gid: <現有 gid>}
  cortex-reviewer-planner: {uid: <…>, gid: <…>}
  cortex-builder: {uid: <…>, gid: <…>}
  cortex-gate: {uid: <…>, gid: <…>}
service_accounts:
  cortex-egress: {uid: <…>, gid: <…>, home: <現有 home>}
operator_account: <operator>
# external_reader_account、providers.builder 視需要
```

overlay 存成 root 擁有、operator 可讀、不可被其他帳號寫入的持久檔：plan 依 `trust-root-transactional-install.md` §2 在非 root 空白環境執行，必須讀得到它（overlay 不含機密）。

```bash
/usr/bin/sudo /usr/bin/install -o root -g root -m 0644 host-overlay.yaml \
  /var/lib/cortex-installer/host-overlay.yaml
```

之後每一次升級都必須用**同一份** overlay 產生 plan，prior-receipt 交接才會產生相同的帳號 step。

## 3. 擷取並審核 legacy inventory

```bash
/usr/bin/sudo /usr/bin/install -d -o root -g root -m 0755 /var/lib/cortex-installer/legacy
cortex_root_cli install trust-root legacy inventory \
  --config "$cortex_install_config" \
  --host-overlay /var/lib/cortex-installer/host-overlay.yaml \
  --bundle "$cortex_bundle" \
  --output /var/lib/cortex-installer/legacy/capture.json
```

- 已有 receipt、maintenance snapshot 或 lease marker 的主機會被拒絕（應改走 `--prior-receipt`）。
- 輸出位置不得落在 roots、受管路徑或帳號 HOME 之下，也不得經過 symlink；既有檔案不會被覆寫。
- 結果必須是 `census_stable: true`；`false` 表示擷取期間有物件被換成 symlink 或 inode 改變，找出原因後重新擷取。
- 以回報的 `inventory_sha256` 重新命名為 `/var/lib/cortex-installer/legacy/<inventory_sha256>.json`（root 0644，operator 可讀），作為之後 plan 與 apply 綁定的正本。

審核：

```bash
"$cortex_cli" install trust-root legacy show --inventory /var/lib/cortex-installer/legacy/<inventory_sha256>.json
```

逐項確認：帳號 row（uid／gid、群組成員、密碼鎖定）、各 principal 在宣告可寫資產以外的可寫路徑（census 例外）、state 頂層與受管目錄內的未列管項目、credential 類物件（只有 metadata，不含內容）。

## 4. 產生 legacy plan

在 overlay 加上 `legacy_adoption` 區塊：

```yaml
legacy_adoption:
  inventory_sha256: <inventory_sha256>
  quarantine_root: /var/lib/cortex-installer/legacy-quarantine
  census_exceptions:          # operator 明示接受、plan 未列管的 job 可寫目錄（可省略）
    - {path: <路徑>, principal: <cortex 帳號>}
  quarantine_paths: []        # operator 額外指定 quarantine 的物件（可省略）
```

```bash
env -i HOME="$cortex_plan_home" PATH="$cortex_bootstrap_root/venv/bin:/usr/bin:/bin" \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONNOUSERSITE=1 \
  "$cortex_cli" install trust-root plan \
  --config "$cortex_install_config" \
  --host-overlay /var/lib/cortex-installer/host-overlay.yaml \
  --bundle "$cortex_bundle" \
  --legacy-inventory /var/lib/cortex-installer/legacy/<inventory_sha256>.json \
  --output "$cortex_plan_path" >"$cortex_plan_result"
```

plan 會為每個物件推導 disposition（adopt／adopt-in-place／quarantine-then-create／quarantine／失敗）；出現 `unclassified`、census `unstable`、未宣告的可寫路徑、帳號不符或 uid／gid 被外部身分持有時，一次列出全部原因並失敗。

之後依 `trust-root-transactional-install.md` §2 做**三方 plan SHA 確認**並把 plan durable 發布。人工審核除了該節列的項目，另外確認 `legacy_adoption` 區塊的 quarantine 清單與 adopted 清單。

## 5. 停止服務並 apply

依 `trust-root-transactional-install.md` §3 取得 maintenance lease、停止三個 service；apply 改帶 `--legacy-inventory`（與 `--prior-receipt` 互斥）：

```bash
cortex_root_cli install trust-root apply \
  --plan "$cortex_plan_path" \
  --confirm-sha256 "$cortex_confirmed_plan_sha" \
  --receipt "$cortex_receipt_path" \
  --legacy-inventory /var/lib/cortex-installer/legacy/<inventory_sha256>.json \
  --maintenance-token "$cortex_maintenance_token"
```

apply 在停服務之後、第一個 mutation 之前重新擷取 inventory，比對穩定欄位 digest、host binding 與 scope，並確認 census 沒有新的可寫例外、沒有 in-flight job、沒有任何 active 的 cortex unit 或 template instance；任一不符就失敗，receipt 維持 planned、journal 為空。

帶 legacy 區塊的 receipt 以 schema v3 寫出；較舊的 installer 會在載入時明確拒收。

## 6. rollback（apply 失敗或決定放棄時）

**必須在 credential 匯入與 activate 之前做**。新服務啟動後會寫入 runtime state，rollback 依設計把它們視為 unknown 並保留，post-activation 的 rollback 因此不會是 restore-safe，需要 operator 裁決。

```bash
cortex_root_cli install trust-root rollback \
  --receipt "$cortex_receipt_path" \
  --maintenance-token "$cortex_maintenance_token"
```

rollback 反向處理 journal：以 `RENAME_NOREPLACE` 把 quarantine 物件搬回原位（原位被佔則保留在 quarantine 並列為 drift）、移除本 receipt 建立的 clone／venv slot／credential 目錄（先搬進 quarantine root 下的私有 staging，驗證 inode 與樹 digest、確認沒有掛載點後才刪）、還原受管目錄 metadata（含空權限的 ACL 條目）、unit 有變動時 `daemon-reload`，最後以 plan scope 重新擷取 inventory。

只有 `legacy_restored: true` 且 `restore_safe: true` 才算回到接手前的狀態，這時恢復原本 active 的舊 service。任何一項為 false 時，service 維持停止、marker 與 snapshot 保留，由 operator 依報告裁決；不得手動刪 receipt 或 quarantine。

## 7. credential 重新匯入、activate、verify

legacy 的 credential 已在 quarantine 內，可直接以 quarantine 內的檔案作為 `--source`，依 `trust-root-transactional-install.md` 的 credential import 步驟逐一匯入 plan 的 `required_credentials`；接著 `activate` → `verify`，verify 通過後 receipt 成為 applied＋qualified。

## 8. 之後的升級

以本次的 adoption receipt 作為 `--prior-receipt`，並用**同一份** host overlay 產生新 plan；新 plan 不會再有 `legacy_adoption` 區塊，新 receipt 為一般的 v2。

## 9. quarantine 的保留

quarantine 內的 legacy 物件保留到**後繼的 qualified receipt 成立後至少 30 天**。在那之前不得刪除；自動 purge 指令尚未提供，需要清理時另行開票，以後繼 receipt 與日期為前提人工處理。

## 10. 已知限制

- Manager 主機的 git 必須 ≥ 2.43（`--attr-source` 搭配 merge-tree，#1142）；此限制屬 delivery，與本流程無關，但升級後會影響 dogfood 的 main-sync probe。
- credential 目錄是先建立、後寫入紀錄；兩者之間若 crash，該目錄不會被記錄，rollback 會停在 blocked 而不是誤刪。
- rollback 刪除 staging 內已驗證的樹時，若有 writer 在搬進 staging 之前就持有樹內的 descriptor，仍可能在逐條 unlink 的空檔寫入；manifest 檢查把風險縮小到單一條目，但未完全消除。
