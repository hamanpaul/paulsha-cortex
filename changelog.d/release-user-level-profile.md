# #1371 使用者層級 release profile

Release workflow 新增 `user-level` 發版模式，預設仍為 `trust-root`。使用者層級模式保留來源、版本、分支、policy 與 PR 治理檢查，略過 RC／legacy-adoption qualification，只發布重建 wheel，且 release notes 會附上不支援 Trust Root system deployment 的固定聲明。Trust Root installer ingress 維持要求 wheel、install-input archive 與 qualification manifest。
