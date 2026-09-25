---
status: draft
work_item: planning-review-source-receipt
---

# Plan review source receipt 設計（#1045）

## Decisions

## D1. Owner 與預期模組邊界

Manager 是唯一 source/review/freeze 決策者。預期實作邊界是：

- manager.py：在最後 plan card 判定點載入可信來源快照、讓 gate 審查同一批 bytes、組 receipt、提出 transition，並在 commit/read-back 前禁止 build dispatch。
- work_bridge.py：若 Manager 目前無法取得原始 WorkAuthority snapshot，只提供唯讀的 exact snapshot resolver。它回傳來源材料，不判定 review 是否 ready，也不寫 workflow state。
- registry.py：新增受限 Manager transition API，只比較 exact old run tuple 並一次保存 receipt reference、完整 baseline、review flag 和 phase/step 變更。API 不讀外部來源。

這是預估模組範圍，不代表現有 API 已提供 immutable per-file provenance 或 CAS。T0 必須依目前呼叫路徑確認可重用的 provider/source 接口；如必須再增加 production module、持久化欄位或第二個 state writer，停止並重新 sizing/split。避免改 WorkflowRun schema：使用既有 evidence_refs 存放 receipt 的 content-addressed reference；若此路徑無法提供 hash 綁定和 read-back，先重新定義最小持久化方案再 intake。

## D2. 來源解析與 bytes 一致性

Plan review 的輸入不能只取自 _load_run_planning_artifacts 的 workspace bytes。Manager 先取得本次受信任 WorkAuthority snapshot，解析 canonical spec/design/todo 的 ref/kind/work_id 和來源 revision，並從 exact source revision 讀出 bytes；Git-backed provider 同時核對 blob object ID。對不提供 Git object database 的 provider，使用 provider 的 immutable revision identity 加可重算內容 SHA-256。來源列均綁同一 WorkAuthority digest/snapshot；WorkflowRun.source_revision 只表示 WorkAuthority authority digest，不能當 Git tree/commit。

同一組 bytes 建成 review artifacts、逐檔 receipt rows 和新的 PlanningArtifactAuthority。Manager 在 review 後、CAS 前再讀 exact sources 或驗證 immutable snapshot lease，確認所有列的 bytes 仍一致。不同 revision、path/type ownership 不符、duplicate/unknown row、symlink/escape、unmerged source、內容 hash 不同或 provider 無法重現時，回明確拒絕，不能 fallback 到目前工作目錄。

## D3. ReadyPlanReviewReceipt 格式與存放

receipt 是 Manager 產生的 immutable evidence document，不是 plan_review_passed 的另一個名稱。Canonical JSON 包含 R2 的 run/work/card/result binding、WorkAuthority digest/provider snapshot、按 ref 排序的 per-file source rows、review input digest 及 receipt schema version。Gate outcome 必須原樣正規化為 ready=true、failed_check=null、terminal=false、ordered checks_run、observations digest；不能由先前 boolean 或成功 phase 重建。

以 canonical payload 的 SHA-256 作內容地址，寫到 coordinator evidence 下的 plan review receipt namespace，採 no-follow、exclusive create 和 flush/read-back。相同 digest path 只可重用 exact bytes；不同 bytes 代表衝突。receipt 在 Registry CAS 前存在；若 CAS 失敗，孤兒檔案不被 WorkflowRun 引用且不代表 rebind 成功。

## D4. Transition 次序與 compare-and-swap

順序固定：

1. Manager 取得 active exact run 和一份可信 WorkAuthority snapshot，確認 repo/work_id/claim/source binding。
2. 解析完整 canonical artifacts，驗證安全路徑與每檔來源列；plan review 消費這些 bytes。
3. 僅最後 plan card、Yellow band、gate outcome 非 None 且 ready=true 時建立 receipt。其餘結果走 R4，不寫 receipt、不 rebind。
4. Manager immutable-write/read-back receipt。
5. 呼叫 Registry 專用 API，攜帶 expected run_id/work_id/repo/claim/source, old phase/step/generation, old planning_authority/source_revision，以及 exact new authority/revision/receipt ref/step/phase/attempts。Registry 在單次受限 transition 中重新比較本地 current tuple；任一不同即 typed conflict 且零欄位改動。
6. Manager read-back WorkflowRun，逐欄比對完整新 baseline、receipt reference、plan pass、phase/step/attempts；只有完全相等才進 build dispatch。

這不宣稱 evidence file 和 Registry 是跨 store transaction：immutable receipt 先寫，Registry row 是唯一授權發布點。未被 run 引用的 receipt 不改變 workflow state；Registry transition 只會整體成功或完全不寫。CAS conflict 後下次 tick 從新 source snapshot 與 gate 證據重算，不拿舊快照自動重試。

## D5. Band 與 gate 決策矩陣

| Band / review result | 新 receipt | baseline rebind | plan pass | build |
|---|---:|---:|---:|---|
| Yellow，最後 plan review 明確 ready=true，來源證明與 CAS 均精確通過 | 建立並引用 | 原子提交完整新集合 | 同一 transition 設定 | read-back 後才派 |
| Yellow，gate 回 None | 不建立 | 不做；若 drift 必須 rebind 則 fail closed | 不設定 | 保持既有 fail-soft routing，不能因此得到 rebind |
| Yellow，rejection/non-terminal/terminal 或輸入不完整 | 不建立 | 不做 | 不設定 | 沿既有 stop/retry/human route |
| Green 或 sizing band None | 不建立 | 不做；若 drift 必須 rebind 則 fail closed | 不設定 | 無 drift 時保留原路徑，不鑄造 review pass |
| 任一 band，但 source/hash/path/revision/CAS/read-back 不符 | 不建立或不引用 | 不做 | 不設定 | 不派 |

表中 build 欄的既有 routing 不授予 rebind。#1045 只在完整 ready receipt transition 完成後提供新 baseline。

## D6. 測試和失敗處理

RED tests 先鎖 positive T6 todo-text correction、exact bytes equality 和同次 CAS/read-back，再實作 source resolver 與 transition。負例逐項注入 source omission/duplicate/wrong ref-kind-work_id/unknown provider revision/unmerged source/symlink/traversal/hash drift、review None/rejection/non-terminal/Green/no-band、post-review artifact mutation、receipt collision、old-run tuple/CAS race、persisted read-back mismatch。每個負例斷言 baseline、planning_source_revision、plan_review_passed、phase、steps/attempts 不部分改動且 builder dispatcher 未呼叫。

測試另保留無 drift 的 Green/no-band 舊 routing，以及 Yellow gate None 不設定 plan pass 的相容性；不得把相容性測試寫成 rebind 通過。#1046 的 candidate-preserving recovery、verify dispatch 和 drift-stop taxonomy 不列入此票測試 owner。

## D7. 已拒絕方案

- 只比較 run/workspace SHA：不能證明檔案來自受信任 WorkAuthority source。
- 將 WorkAuthority digest 當 Git commit：它是 source authority digest，兩者不可互換。
- gate ready 後再讀另一份 workspace 檔案來 freeze：reviewed bytes 與 frozen bytes 可能不同。
- 從 plan_review_passed、phase、一般 gate evidence 推導 ready receipt：legacy flag 不足以證明來源和 review inputs。
- 由 Registry 查 provider/GitHub 或解讀 review：越過 Registry 的受限本地 CAS 責任。
- 先更新 baseline/phase 再補 receipt，或 CAS 未確認就派 build：造成部分 rebind 或未證明派工。
- 把 #1046 的舊 candidate recovery 合進來：該能力依賴本票完成後的 durable receipt 契約，且有獨立 stop/eligibility owner。
