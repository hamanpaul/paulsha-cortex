---
status: accepted
work_item: sizing-stability-direction
domain_breadth: 0
state_consistency: 0
---

# Tasks

本 child ledger 只記錄本次 Candidate 的 pre-archive 交付；母 issue、凍結的
planning-authority plan，以及 Manager-owned archive／merge／runtime 仍不在本卡
完成範圍。

- [x] T1 修正 `planning._spec_stability_risk` helper docstring，明示 typed assessor 前提、結構檢查邊界與不提供任意 dataclass 內容信任邊界；保留現有 structural checks 與 production mapping。
- [x] T2 新增隔離 `tmp_path` regression：legacy `WorkflowRun` 缺少 sizing 欄位時，frozen planning authority/source revision、attempt/history/evidence refs 在 read/reload/fresh object 後保留，且不回填新算法欄位。
- [x] T3 新增隔離 `tmp_path` regression：CompletionRecord 與 immutable verification evidence 的 bytes、raw SHA-256 與 canonical hash 在 fresh reads 後穩定；history/evidence mutation negative controls 確認 oracle 會失敗並由 read-time hash check 拒絕。
- [x] T4 新增 `current_sizing_snapshot` 的 single/cross-scope complete/missing/blocked 成對 score+band matrix，並覆蓋 invalid domain/state 的 `(None, None)` fail-soft；保留既有 claim/retry、pure-function 與 band boundary tests。
- [x] T5 更新本票 changelog fragment、`CHANGELOG.md [Unreleased]` 與本 child ledger；本卡不宣稱 archive、merge、issue closure 或 runtime/install loaded revision。
- [x] T6 修補歷史回歸 oracle：以完整 persisted v2 `WorkflowRun` baseline 比對 first/fresh reload，保留 legacy sizing 欄位缺席語意，並以實際 frozen planning bytes 與 matching SHA 驗證 non-null versioned fixture；只補 pre-archive tests，不改 production 或 pinned planning input。
- [x] T7 本卡以 non-ff merge 對齊 canonical `main`，保留 Candidate 與主線雙方的 Unreleased changelog entries 與上游 artifacts；此為 pre-archive 整合，不宣稱 Manager-owned delivery merge、archive 或 issue closure。

PENDING — Manager 的獨立 review、exact-head delivery preflight、CI/remote checks、archive、merge 與 issue closure。

PENDING — Manager/operator 的 installed/runtime loaded revision、sibling release、母 issue 重評與正式完成投影。
