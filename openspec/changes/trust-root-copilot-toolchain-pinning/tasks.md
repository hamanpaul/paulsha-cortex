---
status: accepted
work_item: trust-root-copilot-toolchain-pinning
---

# Tasks

- [x] 1.1 RED：依 `docs/superpowers/plans/trust-root-copilot-toolchain-pinning.md` 新增 `tests/test_trust_root_copilot_toolchain_pinning_681.py`，鎖定 pinned wrapper 路徑、sanitized PATH/HOME、version metadata、symlink/traversal fail-closed 與 idempotent reinstall 缺口。
- [ ] 1.2 GREEN：讓 Copilot wrapper 解析 pinned binary/tree、拒絕 symlink/path escape，缺版本或版本不符時 fail-closed，且維持 operator/job 身分隔離。
- [ ] 1.3 VERIFY：把 wrapper 納入 generated-vs-installed attestation，跑 focused tests、`python3 -m pytest -q`、policy/preflight、review、delivery、CI，並關閉 #681。
