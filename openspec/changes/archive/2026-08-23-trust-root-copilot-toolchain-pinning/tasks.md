---
status: accepted
work_item: trust-root-copilot-toolchain-pinning
---

# Tasks

- [x] 1.1 RED：依 `docs/superpowers/plans/trust-root-copilot-toolchain-pinning.md` 新增 `tests/test_trust_root_copilot_toolchain_pinning_681.py`，鎖定 pinned wrapper 路徑、sanitized PATH/HOME、version metadata、symlink/traversal fail-closed 與 idempotent reinstall 缺口。
- [x] 1.2 GREEN：讓 Copilot wrapper 解析 pinned binary/tree、拒絕 symlink/path escape，缺版本或版本不符時 fail-closed，且維持 operator/job 身分隔離。
- [x] 1.3 VERIFY：把 wrapper 納入 generated-vs-installed attestation，重跑
      focused trust-root/launcher tests、`python3 -m pytest -q` 與帶 PR
      上下文的 `policy-check`，並以 conventional commit 提交 tested
      descendant candidate（僅涵蓋 pre-archive builder repair）。

      Pre-archive status for this card: active OpenSpec tasks now stop before
      archive. This repair reran the focused trust-root ACL/toolchain
      regression subset, full `python3 -m pytest -q`, and authoritative
      `python3 -m paulsha_cortex.preflight_ci --pr 789` against the current
      candidate boundary. Independent review, delivery, CI, archive, merge,
      issue closure, and done remain Manager actions.
