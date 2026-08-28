---
status: accepted
work_item: trust-root-agy-builder-grant
---

# Tasks

- [x] RED：新增 `tests/test_trust_root_agy_builder_grant_805.py`，以 overlay 宣告
      `agy/gemini-3.7-flash-high` 的 `build` identity，逐一驗證缺少 launcher
      profile、builder toolchain grant、builder credential grant 時 fail-closed，
      並要求診斷點名缺失層。
- [ ] 實作 Trust Root AGY builder toolchain grant，並在 builder credential registry
      加入 `("agy", HOME_REDIRECT_TREE)`；憑證匯入須逐 principal 明示，不得探索或複製
      其他 principal 的 `$HOME` 狀態。
- [ ] 實作共用的 persona–executor compatibility check，接入 model resolution、doctor
      與 dispatch preflight；契約不完整時須在 launch 前拒絕。
- [ ] 更新 four-way permission generator、generated/install attestation 與 Trust Root
      Phase 2b runbook，維持 planner/reviewer read-only 與 independence-domain 邊界。
- [ ] 補齊 overlay identity、planner/reviewer non-regression、PATH/OAuth isolation 與
      live qualification 前不得宣傳 packaged AGY fallback 的回歸測試。
- [ ] 執行 focused/full repository gates、policy/preflight 與獨立 review。
- [ ] 於 merge 後以 exact SHA 執行 Trust Root AGY builder acceptance。
