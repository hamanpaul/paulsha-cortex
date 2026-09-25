---
status: draft
work_item: workflowrun-authority-restart-cas
issue: 1068
parent_issue: 1055
openspec_change: workflowrun-authority-restart-cas
domain_breadth: 0
state_consistency: 2
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
applicable_contract_rules:
  - R-09
  - R-16
  - R-19
---

# 既有 Candidate WorkflowRun authority restart CAS 設計（#1068）

本設計與 `workflowrun-authority-restart-cas` 規格及 Todo 對應，定義 registry owner 的單一 transition。規劃基線為 `origin/main` HEAD `6a32a3e5e0af841794f340313c11f60f2999f6ae`。截至 2026-09-25，#966 與 PR #1067 仍為 OPEN；以下 API 是待 #966 合併後對齊的契約，不宣稱目前 `origin/main` 已具備該能力。

## D1 — Owner 與依賴順序

#1068 只擁有 `paulsha_cortex/coordinator/registry.py` 中一個 registry-layer WorkflowRun transition。必須先等 #966 landed；不得依 PR #1067 尚未合併的 head 實作。#966 合併後，實作 owner 必須檢查 landed revision read/CAS surface，並以該 public contract 為依據。若 #966 只提供 per-method `_persist()` CAS，無法讓 exact tuple check 與 mutation 成為同一 transaction，就停止並提出 issue-backed re-scope；不得把 `_loaded_revision` 當 private API 讀取、另加 raw writer，或複製其 transaction lock。

#1054 與 #1063–#1065 仍是較高層 Manager integration 必需的 authority/freshness 輸入。#1069 擁有 `work_actions.py` permission 與 action ordering；#1070 擁有 delivery journal 工作。本設計不定義或實作這兩層。

## D2 — Request 格式與 immutable comparison

Registry transition 接受的 typed request 包含：

- 經 landed #966 contract 提供的 expected durable registry revision。
- 目標的完整 expected identity/state：run ID、repo、work ID、status、phase、retry classification、claim key、source revision、Candidate、verified head、verify/review gates 與 evidence refs、PR refs，以及已觀測的 active-job set（必須為空）。
- 唯一 request ID 與完整序列化 request payload 的 digest。Digest 綁定 expected tuple 與要求的新值，但本身不是 authority proof。
- 完整的新 claim/source binding，以及由已載入並驗證 authority 的 caller 提供的完整 `WorkAuthority` digest。

Registry 必須精確比較所有值。它不解析任何 source、不接受部分 mapping、不從目前 row 補齊缺少的 tuple 成員、不正規化 ambiguous values，也不從 caller 選取的欄位推導 authority digest。Request 綁定一個既有 run 與一個 Candidate。

## D3 — Atomic transition boundary

Registry-owned operation 在單一 durable CAS boundary 內依序完成：

1. 依 persisted restart audit/receipt 判斷 exact request replay。相同且已完成的 request 回傳歷史結果，不重寫目前 state。相同 ID 搭配不同 digest 時回傳 typed conflict。
2. 確認 durable revision 等於 expected revision，且目前完整 WorkflowRun/job tuple 等於 request 的 expected snapshot。Revision 或任一欄位不同時 fail-closed。
3. 確認目標仍是同一個 ongoing Candidate run、舊 claim/source 值相符，且 active-job set 為空。
4. 以目前 durable row 建立下一份 WorkflowRun snapshot，只替換目前 claim/source binding、將 phase 設為 verify、invalidate verify/review gates、清除 `verified_head`，並附加一筆含 request ID 與 payload digest 的 immutable authority-restart audit。
5. 透過 landed #966 CAS 將完整 registry snapshot 持久化一次。不得另外提交中間 gate reset、request receipt 或 audit。

#966 landed 後才能選定實際 implementation seam。若沒有受支持的 operation 能防止 concurrent writers 介入 compare、mutation 與單次 commit，本 child 必須停止，不能弱化 R2。

## D4 — 保留資料與 evidence

Transition 只改變目前 authority era 與 verify/review gate state。它保留 run identity、Candidate SHA、build/repair step records、builder jobs 與其 evidence、舊 claim-era JobRegistry bindings、PR refs、無關 gates 與既有 history。不 dispatch job、不碰 delivery journal、不查詢 GitHub、不編輯 evidence files。新一輪 verification/review 是由 #1069 負責的後續 Manager action。

## D5 — Conflict、rollback 與 replay 結果

Revision mismatch 或 tuple mismatch 必須回傳 typed registry conflict。Durable bytes 不得收到 authority-restart mutation。Registry memory 遵循 #966 conflict recovery，還原到 authoritative durable snapshot；遭拒的 mutation、重設 gates 與 audit 不得留在 memory。Atomic write failure 遵循 #966 rollback contract，且不得回報 transition 已套用。沒有 automatic merge、retry 或第二次 transition。

只有 persisted record 證明相同 request ID 與完整 payload digest 已提交時，exact replay detection 才可先於原 expected tuple 的 drift rejection。Replay 是唯讀操作：若後續 verification/review 已寫入新 gate evidence，replay 必須保留它。同一 ID 搭配變更後的 payload 仍是 conflict。

## D6 — Fixture 與隔離策略

Restart/replay 案例使用隔離的 temporary `JobRegistry` state 與 fresh registry instance。#1069 指定的 #983 run ID、Candidate SHA 與 PR number 只作 synthetic fixture 值；fixtures 不檢查 live control roots、GitHub、Todo、Monitor、WorkAuthority 或 journal state。每個 case 比較前後 raw bytes、parsed workflow state、job records、evidence refs、history 與 sequence。

預期 test matrix：

| Case | 預期結果 |
|---|---|
| Exact snapshot、無 active job、完整 digest | 同一 run 一次 durable transition 與一筆 audit |
| Stale raw revision 或任一 tuple 欄位變更 | Typed conflict，不寫入 transition bytes |
| Active job 或 Candidate 缺漏 | Typed conflict，不寫入 transition bytes |
| Atomic persistence failure | 沒有 partial transition；結果遵循 #966 rollback |
| 相同 request ID 與完整 payload | 回傳原結果，不做第二次 write 或 audit |
| 相同 request ID 搭配不同 payload | Typed conflict |
| Registry reload 後 exact replay | 回傳原結果，不清除 gate evidence |
| #983 型 identity/Candidate/PR fixture | 相同 assertions；只用 fixture |

## D7 — 範圍與停止條件

Production scope 是單一 module `paulsha_cortex/coordinator/registry.py`。Tests 使用隔離 fixtures。若 landed #966 public API 不足、必須修改第二個 production module、caller 無法提供 exact active-job facts，或 audit/idempotency 無法放在同一 registry transaction，便停止。改變 owner boundary 前先透過 linked issue 重定範圍。

不得在此實作 `work_actions.py`、Manager permission/freshness/dispatch、WorkAuthority loading、delivery journal operation、#983 runtime action、GitHub API call 或 candidate-less recovery。未來 registry method 本身不得 push、open、update 或 merge product PR。
