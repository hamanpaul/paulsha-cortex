---
status: accepted
work_item: installer-shared-config-guard
---

# Tasks

- [x] RED 測試：append-not-replace（多 workspace 保留＋新增條目＋`.bak-*`）、agents-root/HOME 不一致 fail-loud、identities 不可載入 raise。
- [x] 實作 `_migrate_instance_config` 雙路徑與備份；實作 agents root 一致性檢查（顯式 `--agents-root` 豁免）。
- [x] 修復 retry 發現的 process-env default agents root 跨 HOME 守衛，以及既有 project／identity config 不可載入時拒絕替換。
- [x] 修復 verifier 發現的任意 basename HOME containment、session fixture tmp 隔離、UTC backup timestamp，並移除 installer dead code。
- [x] 修復 review 發現的既有 workspace 逐字保留、monitor/hippo 設定 round-trip，以及 migration rollback 備份清理。
- [x] 修復 delivery review 的 HOME 診斷訊息、既有設定檔權限保留、備份建立時的原子權限、porcelain 安裝指引、migration flock，以及 YAML round-trip 的 pre-archive 文件說明。
- [x] 修復 porcelain `_run_install` 在 installer 正常返回時遺失 stderr 的轉送，並補 regression test；本卡僅完成 pre-archive 修正，archive／merge 由 Manager 處理。
- [x] 修復 pre-archive review 的 migration rollback data-loss 缺口、驗證失敗不留 lock，以及 porcelain stderr forwarding 的單一路徑收斂；archive／merge／issue closure 仍由 Manager 處理。
- [x] 修復 migration restore 失敗時的備份路徑診斷，並以 `restore_ok` 控制成功後的備份清理；archive／merge／issue closure 仍由 Manager 處理。
- [x] 修復 review #7 的無備份 rollback 診斷與 umask 下的備份 mode 保留，並補 0600／0644 與無備份情境 regression tests；本卡僅完成 pre-archive 修正，archive／merge／issue closure 仍由 Manager 處理。
- [x] `tests/conftest.py` session fixture 隔離 `PSC_AGENTS_ROOT`／`PSC_PROJECT_CONFIG_ROOT`。
- [x] 既有 installer 測試回歸綠；補 `changelog.d/` 碎片與 README「install service 對既有 config 只 append」說明。
- [x] focused／full gates；完成 pre-archive Candidate 準備，後續 evidence canonicalization、archive 與 merge 由 Manager 處理。
