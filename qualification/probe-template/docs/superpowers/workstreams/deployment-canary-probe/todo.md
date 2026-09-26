---
status: accepted
work_item: deployment-canary-probe
---

# Deployment canary probe

## Boundary

- 只改 `src/canary_probe.py`、`tests/test_canary_probe.py`、`README.md`、`CHANGELOG.md`、
  `changelog.d/deployment-canary-probe.md` 與本 todo／OpenSpec tasks 的勾選狀態。
- 需求與設計見 `docs/superpowers/specs/deployment-canary-probe-spec.md`、
  `docs/superpowers/specs/deployment-canary-probe-design.md`。

## Tasks

- [ ] 新增回歸測試：`normalize_label("   ")` 回傳 `"unnamed"`，保留既有 trim 測試。
- [ ] 修改 `normalize_label`：去除前後空白後為空字串時回傳 `"unnamed"`。
- [ ] 在 `README.md` 的 Usage 段落記載新行為。
- [ ] 新增 `changelog.d/deployment-canary-probe.md`，並在 `CHANGELOG.md` 的 `## [Unreleased]`
  加一行含 `deployment-canary-probe` 的條目。
- [ ] 勾選本檔與 `openspec/changes/deployment-canary-probe/tasks.md` 的全部項目。
- [ ] 執行 `.project-policy.yml` 的 preflight 指令並全部通過。
