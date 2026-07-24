---
status: accepted
work_item: release-pipeline
---

# Design

## Decisions

- **擴充既有 tests.yml**：Python matrix 直接加在既有 job，不新建重複觸發的 workflow。
- **build/smoke 以 artifact 銜接**：smoke-install job 依賴 build job 產出的 wheel artifact，不重複打包。
- **release.yml 觸發唯一化**：僅 tag `v*` push；不呼叫 PyPI 發布。
- **SHA pin 統一格式**：`owner/action@<sha> # vX.Y.Z`，比照既有 workflow。
- **不動 policy-check.yml 引擎 pin**：維持 R-23 attestation 一致性。
- **persona write_paths 例外顯性化**：`.github/workflows/**` 超出既有 builder write_paths，需 shadow 模式放行並知會 reviewer。
