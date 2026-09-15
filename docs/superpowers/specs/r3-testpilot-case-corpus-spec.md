---
status: accepted
work_item: r3-testpilot-case-corpus
issue: 904
---

# R3 testpilot case 素材盤點第二輪 — Spec

對應 issue [#904](https://github.com/hamanpaul/paulsha-cortex/issues/904)，承接 #667（PR #678）
第一輪盤點後 `docs/superpowers/workstreams/r3-testpilot-case-corpus/todo.md` 剩下的 6 項未勾任務。
本 workstream 的產出**只有文件**：更新候選清單與 todo，不寫任何 case yaml、不建 harness、不動
`paulsha_cortex/`、不開 R3 實作票。

## Requirements

- **R1 補讀 08-12 波 6 張**：讀 issue #473／#475／#476／#478／#506／#508 的本文與留言，將發現併入
  `case-candidates.md`：已在候選者（`#478`→`recovery-reports-ok-while-git-registry-stale`、
  `#506`→`secondary-rate-limit-response`）更新 `hit_by` 並補證據行；新增的候選依既有格式
  （id／症狀／子系統／生命週期階段／artifact 型別／oracle 型別／evidence）加入，deck 與
  work-registry schema 兩格若成立須各至少一筆。
- **R2 ship／delivery 語意面**：不掃 issue；改讀 `paulsha_cortex/coordinator/github_delivery.py`
  的五個表面——PR metadata preflight、merge authorization、delivery journal、push readback、
  closed-unmerged PR——每個表面至少產出一筆候選或一筆 evidence-insufficient，fixture 來源標註
  `~/.agents/coordinator-cortex/delivery-journal.json` 內對應 run 的 run_id（唯讀，不複製內容）。
- **R3 porcelain 繞過手法**：不掃 issue；改讀 `docs/` 的 onboarding／quickstart／troubleshooting
  與 driving-cortex skill 相關文件（#177／#192），產出候選並在每筆標明「穩定行為」或「operator
  繞過」。
- **R4 T1 三筆決定**：對 `review-identity-loader-asymmetry`（#490）、
  `porcelain-cli-verb-must-match-permgen-execstart`（#618＋#619）、
  `unbounded-substring-marker-misclassifies-failure`（#487＋#500＋#554）在 `case-candidates.md`
  新增「T1 首批決定」一節，逐筆記錄「是否列為 R3 首批實作票」的決定與理由；**不開實作票**
  （R3 本體等 R2 Compact 收斂）。
- **R5 EvidenceAttestation 契約對齊**：在 `case-candidates.md` 新增「case report ↔
  EvidenceAttestation 契約對齊」一節：subject 綁 candidate、不得自我背書；只記錄依賴與形狀
  要求（引用 `paulsha_cortex/coordinator/verification.py` 中既有的 attestation 欄位名），不實作。
- **R6 harness 契約層硬規則**：在 `case-candidates.md` 新增「case harness 契約層硬規則」一節：
  多 UID 不可用時標 `unsupported`、不得標 `pass`；標明目前無執行機制、屬未來 harness 的契約需求。
- **R7 todo 與計數收斂**：`todo.md` 對應 6 項勾為 `[x]`（每項附一行完成摘要），frontmatter
  `status` 維持 `accepted`（本輪由 operator 於進件時改定），「一句話狀態」段更新為第二輪完成；
  `case-candidates.md` 檔頭的候選總數、`hit_by` 分佈、evidence-insufficient 筆數重新計算且與
  內文一致。

## Problem

第一輪盤點誠實記錄了四個覆蓋缺口（08-12 波 6 張未深讀、ship／delivery 零覆蓋、porcelain
分不出穩定與繞過、deck-combo 次級缺口）與三項待決事項。這些缺口不補，R3 候選清單無法作為
R2 Compact 收斂後開實作票的依據；而三項待決若不落成文字，未來 harness 契約沒有可引用的來源。

## 驗收

- `git diff --stat` 只含 `docs/superpowers/workstreams/r3-testpilot-case-corpus/case-candidates.md`、
  `docs/superpowers/workstreams/r3-testpilot-case-corpus/todo.md`、`changelog.d/r3-testpilot-case-corpus.md`
  與 `CHANGELOG.md`；不得出現 `paulsha_cortex/**`、`tests/**`、任何 `*.yaml` case、
  或其他 workstream／openspec 檔案。
- R1–R6 每項在 `case-candidates.md` 有可定位的章節或條目；R7 的計數一致性以人工核對。
- PR body 含 `Closes #904`。

## Boundary

- 不寫 case yaml、不建 mock provider／tick harness、不動 `paulsha_cortex/`、不預蓋框架、不開
  R3 實作票。
- 不修改 `openspec/changes/**`（本 workstream 無 openspec change）。
- 不修改其他 work item 的 todo／spec／plan。
