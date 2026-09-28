# Trust root legacy adoption

## Why

transactional installer 只承認兩種既有物件的來源證明：本次 receipt，或 `--prior-receipt` 交接的上一個 applied＋qualified receipt。Phase 2b 時期以手工流程部署、從未產生 receipt 的主機因此既不能 fresh install（既有帳號、目錄與 unit 沒有 provenance，uid 也可能與 release config 衝突），也不能 upgrade（沒有 prior receipt）。runbook 又明定不得再照 Phase 2b 文件手工換 venv，這類主機就卡在舊版無法前進（[#1122](https://github.com/hamanpaul/paulsha-cortex/issues/1122)）。

config 早就有必填欄位 `legacy_policy: quarantine|reject`，歸檔設計也把它描述為 plan 內的「legacy quarantine policy」，但目前只驗值並寫進 plan，沒有任何行為。本 change 正式賦予它語意，讓 operator 能以明確審核、可回滾的方式接手沒有 receipt 的舊主機。

## What Changes

- 新增 root 執行、唯讀的 `cortex install trust-root legacy inventory`，擷取舊主機的帳號、受管路徑、權威面物件、service、job 帳號可寫路徑 census 與 state 摘要，輸出自證 digest 的 canonical inventory；另有非 root 的 `legacy show` 摘要。
- 新增 `--host-overlay`：只允許覆寫帳號 uid／gid、egress home、`operator_account`、`external_reader_account`、`providers.builder` 與 `legacy_adoption` 區塊；release 封存的 install config 仍是權威，主機差異顯式可審。
- `plan --legacy-inventory` 依確定性規則為每個物件推導 disposition（adopt、adopt-in-place、quarantine-then-create、quarantine、plan 失敗），plan 綁定 inventory digest、scope 與 host binding。
- 新增 step kind `legacy-quarantine`：以同檔案系統 `renameat2(RENAME_NOREPLACE)` 把衝突或未列管的舊物件搬進 root-only quarantine，**不刪除、不覆寫**任何 legacy 物件；rollback 反向搬回。
- `apply --legacy-inventory` 在停服務後重新擷取 inventory，穩定欄位 digest 必須與 plan 相同；receipt 記錄 legacy provenance，之後的升級可以此 receipt 作為 `--prior-receipt`。
- rollback 後重新擷取 inventory，必須等於原 digest 才回報 `legacy_restored=true` 並納入 `restore_safe`。
- `legacy_policy` 語意：`reject` 維持現行行為；`quarantine` 在沒有 legacy block 時也與現行相同，有 legacy block 時才允許依 inventory 接手。沒有 legacy block 的 plan（包括 release qualification）行為完全不變。
- RC qualification 新增 `legacy-adoption` profile：容器內先布置 Phase 2b 形狀的舊主機，再跑 inventory→plan→apply→verify→rollback；release 含 `paulsha_cortex/trust_root/install/` 或 `qualification/` 變更時必跑。
- runbook 新增 legacy adoption 一節。

前置（另票，不在本 change 範圍）：[#1123](https://github.com/hamanpaul/paulsha-cortex/issues/1123)（receipt 目錄快照無界、getfacl 未加 `-E`、rollback 不 daemon-reload、大檔／symlink snapshot）必須先落地。[#1125](https://github.com/hamanpaul/paulsha-cortex/issues/1125) 已實測證明 #568 的 AGY reviewer 手工 drop-in 已由 launcher `--add-dir` 取代，adoption 將其移入 quarantine 不會讓 reviewer 行為倒退。

## Owner 裁決（2026-09-28，#1122）

- 保留舊主機的 uid／gid，不 remap（release 的 991–995 在參考主機上撞到 systemd-resolve、render、kvm、sgx、input 等系統身分）。
- 主機差異以持久化 host overlay 承載；egress home 不改；`operator_account` 用主機的 operator 帳號。
- **state 只接手必要子集**：plan 宣告受管的 state 目錄連同內容就地接手；plan 未宣告的 state 頂層項目、受管目錄內未列管的子目錄與暫存殘留、job worktree pool、source repo、credential 類物件一律移入 quarantine。
- 不遞迴重寫既有檔案的 ACL，改以各 job 帳號的可寫路徑 census 作為 fail-closed gate。
- credential 移入 quarantine 後以既有 import 流程重新匯入。
- quarantine 保留到下一個 qualified receipt 成立後 30 天，才允許 purge。

## Capabilities

### New Capabilities

- `trust-root-legacy-adoption`：無 receipt 主機的 inventory、host overlay、disposition、quarantine step、legacy provenance、rollback 還原與 RC 覆蓋。

### Modified Capabilities

無（installer 既有行為在沒有 legacy block 時完全不變）。

## Impact

- 程式：`paulsha_cortex/trust_root/install/`（core、backend、cli，新增 legacy 模組）、`qualification/`、`.github/workflows/rc-qualification.yml`。
- 文件：`docs/superpowers/runbooks/trust-root-transactional-install.md`（或獨立 legacy adoption runbook）。
- 參考主機的實際接手由 operator 另行執行，不屬於任何 PR；接手前該主機維持舊版，不做破壞性操作。
