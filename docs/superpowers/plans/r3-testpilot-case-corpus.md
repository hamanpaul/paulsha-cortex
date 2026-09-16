---
status: accepted
work_item: r3-testpilot-case-corpus
issue: 904
---

# R3 testpilot case 素材盤點第二輪 — Plan

需求：`docs/superpowers/specs/r3-testpilot-case-corpus-spec.md`（R1–R7）；設計：
`docs/superpowers/specs/r3-testpilot-case-corpus-design.md`（D1–D6）。docs-only；只改
`docs/superpowers/workstreams/r3-testpilot-case-corpus/{case-candidates.md,todo.md}` 與 changelog。

## Tasks

- [ ] **T1 R1 補讀 6 張**：`gh issue view` #473／#475／#476／#478／#506／#508（含 comments），
      在 `case-candidates.md` 新增「第二輪：08-12 波補讀」一節；更新 #478／#506 既有候選的
      `hit_by`／evidence，新增候選依 D2 格式；deck、work-registry schema 兩格各至少一筆或明寫
      「不成立＋理由」。
- [ ] **T2 R2 ship／delivery**：讀 `paulsha_cortex/coordinator/github_delivery.py` 的五個表面，
      新增「第二輪：ship／delivery 語意面」一節，每表面至少一筆候選或 evidence-insufficient，
      fixture 欄標 `delivery-journal.json` 的 run_id。
- [ ] **T3 R3 porcelain**：讀 `docs/` onboarding／quickstart／troubleshooting 與 driving-cortex
      skill 文件（#177／#192），新增「第二輪：porcelain 穩定 vs 繞過」一節，每筆標「穩定行為」
      或「operator 繞過」。
- [ ] **T4 R4 T1 決定**：新增「T1 首批決定」四欄表（D4），三筆候選逐筆記決定與理由；不開票。
- [ ] **T5 R5＋R6 契約備註**：新增「case report ↔ EvidenceAttestation 契約對齊」與
      「case harness 契約層硬規則」兩節（D4 條列形式），引用 `verification.py` 既有欄位名。
- [ ] **T6 R7 收斂**：重算 `case-candidates.md` 檔頭三個數字；`todo.md` 六項勾 `[x]` 並各附一行
      完成摘要，「一句話狀態」改為第二輪完成；新增 `changelog.d/r3-testpilot-case-corpus.md`
      並同步 `CHANGELOG.md [Unreleased]`。
- [ ] **T7 驗收**：`git diff --stat` 只含 spec「驗收」列出的四個檔案；不含 `tests/**`、
      `paulsha_cortex/**`、`openspec/**`、其他 workstream；PR body 含 `Closes #904`。
