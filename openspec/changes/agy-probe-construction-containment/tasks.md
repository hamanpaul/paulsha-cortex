---
status: accepted
work_item: agy-probe-construction-containment
---

# Pre-archive tasks

- [x] T1 RED：在 `tests/test_model_identities.py` 以 local
      `build_agy_argv` alias fault injection 鎖定 plain `ValueError` 的
      AGY probe containment，並保留原 identity／runner 邊界。
- [x] T2 source containment：在唯一 production module
      `paulsha_cortex/coordinator/model_identities.py` 將 AGY argv 建構納入
      既有 smoke exception boundary；維持 `Exception` 邊界、
      `smoke-failed` 投影、safe argv、prompt/kwargs/timeout/payload 契約，
      不修改 launcher、runtime、registry 或 cache protocol。
- [x] T3 runtime regression：以真
      `build_production_planning_runtime()`、isolated registry、temporary
      worktree/cache 與 fake runner，驗證兩種 roster 順序都保留 ready 的
      非 AGY primary，AGY 建構失敗不成為 ready secondary，且不啟動 AGY
      smoke。
- [x] T4 boundary regression：驗證 direct launcher 的 AGY argv builder
      `ValueError` 仍向 caller 傳播且不呼叫 Popen，並保留
      KeyboardInterrupt/SystemExit 與既有成功、發現失敗、runner/rc/payload/
      fence/token/diagnostic 契約。
- [x] T5 documentation/CLI：更新 lifecycle boundary，說明 probe 建構失敗
      只降 AGY ready、非 AGY runtime 可建但不保證異質 secondary；完成
      candidate 環境 checkout 外的 CLI help smoke。
- [x] T6 changelog/docs：既有 candidate 已包含唯一
      `changelog.d/agy-probe-construction-containment.md` 與
      `CHANGELOG.md [Unreleased]` 對應 entry，branch/owner 與既有 docs
      邊界保持一致。
- [x] T7a tests/CLI：本 repair card 的 focused suite 為 109 passed、16
      subtests，authoritative full suite 為 5656 passed、44 skipped、173
      subtests；`openspec validate --specs` 為 22/22，`git diff --check` 與
      checkout 外的兩個 CLI help smoke 均通過。
- [x] T7b policy：candidate commit 後以 exact PR title/body、空 labels、
      `main` base 與本 branch head 執行 pinned PR-context preflight；
      engine、policy、OpenSpec 與 tests 均回報 PASS，未將裸 policy 或
      focused 結果當成此 gate。

## Pending downstream (not active tasks for this change)

Independent review/adversarial review, Manager-owned archive, remote CI and
review-thread/mergeability checks, PR merge and issue closure, installation or
loaded-runtime verification, and the #824 timeout/dependency evidence remain
PENDING. They are not implementation checkboxes for this child and are not
claimed by this file; archive, merge, closure, and done remain Manager/operator
actions.
