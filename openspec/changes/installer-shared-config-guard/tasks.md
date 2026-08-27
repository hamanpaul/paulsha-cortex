---
status: accepted
work_item: installer-shared-config-guard
---

# Tasks

- [x] RED 測試：append-not-replace（多 workspace 保留＋新增條目＋`.bak-*`）、agents-root/HOME 不一致 fail-loud、identities 不可載入 raise。
- [ ] 實作 `_migrate_instance_config` 雙路徑與備份；實作 agents root 一致性檢查（顯式 `--agents-root` 豁免）。
- [ ] `tests/conftest.py` session fixture 隔離 `PSC_AGENTS_ROOT`／`PSC_PROJECT_CONFIG_ROOT`。
- [ ] 既有 installer 測試回歸綠；補 `changelog.d/` 碎片與 README「install service 對既有 config 只 append」說明。
- [ ] focused／full gates，candidate evidence 記入 Cortex 後交付。
