---
status: accepted
work_item: per-work-model-chain
---

# Tasks

- [x] 1.1 RED：依 `docs/superpowers/plans/per-work-model-chain.md` 的 TDD RED 章節新增測試，確認失敗。
- [ ] 1.2 實作至 GREEN，範圍限於 `docs/superpowers/specs/per-work-model-chain-spec.md` 的 Requirements。
- [ ] 1.3 `changelog.d/per-work-model-chain.md` fragment 與 `CHANGELOG.md [Unreleased]` entry（#205）。
- [ ] 1.4 `python3 -m pytest tests/ -q` 全綠；帶 PR 上下文的 `policy_check` 0 fail；`git diff --check` 乾淨。

## 驗收

為單一 WorkItem 指定模型鏈不影響其他 active run；覆寫於 claim 凍結且 resume／retry 沿用；違反約束時 fail closed；evidence 可稽核解析結果與來源。
