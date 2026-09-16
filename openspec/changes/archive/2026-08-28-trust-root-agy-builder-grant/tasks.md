---
status: accepted
work_item: trust-root-agy-builder-grant
---

# Tasks

- [x] RED：新增 `tests/test_trust_root_agy_builder_grant_805.py`，以 overlay 宣告
      `agy/gemini-3.7-flash-high` 的 `build` identity，逐一驗證缺少 launcher
      profile、builder toolchain grant、builder credential grant 時 fail-closed，
      並要求診斷點名缺失層。
- [x] 實作 Trust Root AGY builder toolchain grant，並在 builder credential registry
      加入 `("agy", HOME_REDIRECT_TREE)`；憑證匯入須逐 principal 明示，不得探索或複製
      其他 principal 的 `$HOME` 狀態。
- [x] 實作共用的 persona–executor compatibility check，接入 model resolution、doctor
      與 dispatch preflight；契約不完整時須在 launch 前拒絕。
- [x] 更新 four-way permission generator、generated/install attestation 與 Trust Root
      Phase 2b runbook，維持 planner/reviewer read-only 與 independence-domain 邊界。
- [x] 補齊 overlay identity、planner/reviewer non-regression、PATH/OAuth isolation 與
      live qualification 前不得宣傳 packaged AGY fallback 的回歸測試。
- [x] 執行 focused/full repository gates 與 policy/preflight，將 candidate 整合至
      最新 `origin/main`，並完成 archive 前的 candidate 驗證。
- [x] 由 Manager 於 archive 前執行獨立 review 並收斂 candidate evidence。

> Merge 後 exact-SHA Trust Root AGY builder acceptance 由 Manager／operator 另行執行，
> 不列為本次 pre-archive change 的 active task。
