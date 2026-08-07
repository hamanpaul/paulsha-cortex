### Added
- **Issue #331：`cortex work migrate` 原子動詞設計（ADR-0002）**：新增
  `docs/adr/0002-work-identity-migration.md`，定義用單一 atomic override
  transaction＋寫入前凍結 authority 的 abandon CAS，把識別遷移（如 `-v2`
  世代熔斷）收斂成 1-2 次 CLI 呼叫，取代現況要靠 5 個 PR、跨近 9 小時手動
  拉鋸 `.cortex/work-items.yaml` 的流程（`#326`–`#330` 實測記錄）。刻意維持
  `claim.py` 既有碰撞不變量與 source-owner-transfer 守門不變，不引入合法雙
  owner 暫態；墓碑機制在新設計下不再需要。純設計文件，不含程式碼變動。
