---
status: accepted
work_item: r3-testpilot-case-corpus
issue: 904
---

# R3 testpilot case 素材盤點第二輪 — Design

## Decisions

- **D1 單一產出檔**：所有新候選、決定與契約備註都寫進既有的
  `docs/superpowers/workstreams/r3-testpilot-case-corpus/case-candidates.md`，以新增章節方式附加
  （「第二輪：08-12 波補讀」「第二輪：ship／delivery 語意面」「第二輪：porcelain 穩定 vs 繞過」
  「T1 首批決定」「case report ↔ EvidenceAttestation 契約對齊」「case harness 契約層硬規則」），
  不另開新檔，避免 monitor 產生新的 fallback work item。
- **D2 沿用第一輪格式**：候選條目沿用第一輪的欄位（id／症狀家族／子系統／生命週期階段／
  artifact 型別／oracle 型別／`hit_by`／evidence），新條目的 `hit_by` 標 `round2:<axis>`；
  已存在條目只追加 `hit_by` 與 evidence 行，不改動既有文字。
- **D3 唯讀取證**：R1 用 `gh issue view <N> --comments`；R2 用 `git show` 或直接讀
  `paulsha_cortex/coordinator/github_delivery.py`；R3 讀 `docs/**` 與 skills 文件；
  `delivery-journal.json` 只讀不複製。任何取證步驟都不得修改 repo 檔案（除 D1 的兩個文件）。
- **D4 決定的記錄形式**：R4 每筆決定為「候選 id｜決定（首批／不首批／待 R2）｜理由（一句）｜
  依賴」四欄表格；R5／R6 為條列的契約要求，每條標明「來源：本盤點」與「執行機制：無（未來
  harness）」。
- **D5 計數一致性**：`case-candidates.md` 檔頭的三個數字（候選總數、`hit_by` 分佈、
  evidence-insufficient）在完成後重算；todo 的「一句話狀態」引用同一組數字。
- **D6 不引入測試**：本 workstream docs-only；不新增 `tests/**`（即使是驗證文件存在的測試），
  驗收由 reviewer 以 diff 範圍與章節存在性人工核對。

## Boundary

- 產品程式碼零變更；`.cortex/work-items.yaml` 零變更（綁定已由 PR #905 完成）。
- 不對 R3 候選做實作可行性驗證（那是實作票的事）。
