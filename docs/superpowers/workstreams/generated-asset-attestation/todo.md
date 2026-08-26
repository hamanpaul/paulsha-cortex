---
status: accepted
work_item: generated-asset-attestation
---

# Generated asset attestation closeout

Issue: `hamanpaul/paulsha-cortex#695`.

## Outcome

舊 PR #790 的 `permgen.build_attestation_inventory()` 不再是權威實作，也不得回灌，
否則會與 transactional installer 建立第二套 inventory。現行單一路徑為：

`build_install_plan()` → `_generated_inventory()` → `LocalInstallBackend.installed_inventory()`
→ `attest_generated_inventory()` → `verify_receipt()` → qualification evidence。

它涵蓋 units、shim、polkit、gitconfigs、toolchain wrappers、environment、enforcement、
service executable identity 與 candidate venv tree，credential surface 只記錄 metadata/hash，
不輸出 credential bytes。本 closeout 另修正 category normalization：executable shebang
漂移 fail closed；polkit standalone comments 只告警。

## Acceptance

- [x] generated 與 installed inventory 由同一 install plan／receipt authority 綁定。
- [x] missing／unexpected artifact、metadata drift 與 functional drift 一律失敗。
- [x] shim／toolchain wrapper shebang 是 functional content。
- [x] polkit 完整獨立 `//`／`/* ... */` 註解是 comment-only warning；inline rule 與
      未閉合至 EOF 的 block 仍 fail closed；`;` 不被誤當成 polkit 註解。
- [x] credential evidence 不含 raw secret content。
- [x] work-item authority 已移除不存在的 OpenSpec 與舊 PR #790 實作指向。
