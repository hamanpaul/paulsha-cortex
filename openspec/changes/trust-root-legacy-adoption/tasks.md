# Trust root legacy adoption 工作清單

## 0. 前置

- [x] 0.1 #1123 落地：receipt 目錄快照與子孫數脫鉤、`getfacl -E`、rollback 後 `daemon-reload`、大檔與 symlink 在事前掃描給出明確錯誤
- [x] 0.2 #1125 查證：#568 AGY reviewer 手工 drop-in 已由 launcher `--add-dir`（v0.1.9 起）與 RC pin 的 agy 1.2.11 取代，不需要 permgen 產物；adoption 直接將其移入 quarantine

## 1. Inventory（唯讀）

- [x] 1.1 新增 legacy 模組：inventory schema v1、canonical JSON、穩定欄位 digest、scope digest、host binding
- [x] 1.2 root collector：帳號（含 group、成員、supplementary、密碼鎖、uid／gid 持有者）、受管路徑 lstat＋ACL（`-E`）、權威面探索規則、service 與 enablement、state 摘要；credential 類只記 metadata
- [x] 1.3 writable census：各 job 帳號以 `access(2)` 判定可寫路徑，排除 symlink
- [x] 1.4 CLI `install trust-root legacy inventory` 與 `legacy show`；已有 receipt／snapshot／lease marker 時拒絕
- [x] 1.5 測試：schema、digest 穩定性、credential 不讀內容、receipt 主機拒絕、census 例外判定

## 2. Plan、overlay 與 disposition

- [x] 2.1 `--host-overlay` allowlist 與 overlay digest；不允許的 key 失敗
- [x] 2.2 `legacy_policy` 語意與 `legacy_adoption` 區塊驗證；無 legacy block 的 plan 位元組不變（回歸測試）
- [x] 2.3 disposition 推導（含「必要子集」規則），`unclassified` 失敗
- [x] 2.4 `legacy-quarantine` step kind、destination 規則、排序、拓撲與 schema 驗證
- [x] 2.5 plan 綁定 inventory／scope／host binding；apply 暫時拒絕 legacy plan 直到第 3 節完成

## 3. Preflight、apply、receipt、rollback

- [x] 3.1 apply 時重新擷取 inventory 並比對；census gate；`--legacy-inventory` 與 `--prior-receipt` 互斥
- [x] 3.2 preflight 的 legacy 帳號 provenance（只放寬 provenance，不放寬 collision 檢查）
- [x] 3.3 quarantine backend：`renameat2(RENAME_NOREPLACE)`、同檔案系統檢查、root-only destination 鏈、replay 語意
- [x] 3.4 `adopt-in-place` 沿用非遞迴 metadata replacement
- [x] 3.5 receipt 欄位與 `InstallReceipt.load` 驗證；`_entry_has_adoption_provenance` 收斂
- [x] 3.6 rollback 搬回 quarantine、`daemon-reload`、重新擷取證明 `legacy_restored`、納入 `restore_safe`
- [x] 3.7 以 adoption receipt 作為 `--prior-receipt` 的升級測試
- [ ] 3.8 purge 指令：需後繼 qualified receipt 且滿 30 天

## 4. RC qualification

- [x] 4.1 Phase 2b 形狀 fixture manifest 與布置腳本
- [x] 4.2 `legacy-adoption` profile：inventory→plan→apply→activate→verify→rollback 與 rollback 證明
- [x] 4.3 release preflight：release 含 installer 或 qualification 變更時要求 exact SHA 的 `legacy-adoption` 通過

## 5. 文件

- [x] 5.1 runbook 新增 legacy adoption 一節（inventory 審核重點、overlay、quarantine 內容、rollback 與取回）
- [x] 5.2 CHANGELOG
