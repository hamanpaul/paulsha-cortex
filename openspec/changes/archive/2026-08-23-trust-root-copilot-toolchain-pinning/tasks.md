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
      archive. This repair reran the focused trust-root regression subset, full
      `python3 -m pytest -q`, and authoritative
      `python3 -m paulsha_cortex.preflight_ci --metadata <temp>`. In this
      sandbox the adapter, openspec, and tests pass, but the backend policy
      step still sanitizes to `/usr/bin/python3 -m policy_check`, which is not
      installed on that interpreter, so the remaining preflight failure is
      environment-bound rather than candidate-bound. Independent review,
      delivery, CI, archive, merge, issue closure, and done remain Manager
      actions.
- [x] 1.4 Post-archive repair：維持 official archive，不回填 active change；補強
      reviewer 指到的三個 `permgen.py` 缺口（只經 `$tmp` + `mv -T` 落位、先保留
      unit PATH 驗 `command -v copilot`、最終 entry 檢查留在 `mv -T` 之後），並
      以 `python3 -m pytest -q` 驗證 descendant candidate。
