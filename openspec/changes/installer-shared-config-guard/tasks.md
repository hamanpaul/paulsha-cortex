---
status: accepted
work_item: installer-shared-config-guard
---

# Tasks

- [x] RED 測試：append-not-replace（多 workspace 保留＋新增條目＋`.bak-*`）、agents-root/HOME 不一致 fail-loud、identities 不可載入 raise。
- [x] 實作 `_migrate_instance_config` 雙路徑與備份；實作 agents root 一致性檢查（顯式 `--agents-root` 豁免）。
- [x] 修復 retry 發現的 process-env default agents root 跨 HOME 守衛，以及既有 project／identity config 不可載入時拒絕替換。
- [x] 修復 verifier 發現的任意 basename HOME containment、session fixture tmp 隔離、UTC backup timestamp，並移除 installer dead code。
- [x] `tests/conftest.py` session fixture 隔離 `PSC_AGENTS_ROOT`／`PSC_PROJECT_CONFIG_ROOT`。
- [x] 既有 installer 測試回歸綠；補 `changelog.d/` 碎片與 README「install service 對既有 config 只 append」說明。
- [x] focused／full gates；candidate evidence 由 Manager canonicalize 後交付。
