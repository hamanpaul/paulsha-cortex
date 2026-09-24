---
status: accepted
work_item: copilot-timeout-new-head-review-rearm
---

# Copilot timeout stop 在 exact-head candidate 更新後的受控恢復規格

對應 [hamanpaul/paulsha-cortex#1021](https://github.com/hamanpaul/paulsha-cortex/issues/1021)。本規格只處理同一 ongoing run 的 `copilot-review-timeout` 舊 HEAD stop，不能藉此改變其他 delivery stop 的語意。

## Requirements

### R1 只允許明示 resume 開啟新 epoch

Manager 收到 operator 明示的 `cortex work resume` 後，只有在唯一 ongoing WorkflowRun、同一 WorkAuthority、目前 candidate 已通過 exact-head verification／ForeignReview，且 delivery journal 保存的 `ship` 是 `needs_human / copilot-review-timeout` 時，才可建立一筆由 Manager 寫入、綁定 run、舊 stop HEAD、新 candidate HEAD 與 WorkAuthority digest 的單次恢復許可。週期性掃描、一般 ship replay、caller 自述、agent 的 review verdict 或直接 journal 寫入均不得建立此許可。

### R2 受理條件以新 HEAD 全面重讀

Manager 在重啟 review epoch 前 MUST 重讀並相符：唯一 canonical run id、run candidate 與 verified head、current WorkAuthority digest、delivery binding、PR number、PR HEAD、Candidate Git tree／preflight tree，以及既有 ForeignReview binding。此票只允許 `old_stop.head != current_candidate`。任何欄位缺失、含糊或漂移都 MUST fail closed，保留舊 stop 並回報具體阻擋原因。

### R3 新 HEAD 只採信本身有效的 review

若新 HEAD 已有有效 Copilot review，沿用 #948 的既有 exact-head adoption 判準及正常 finding／thread 判斷；否則只向此 PR 發出一個新 request，建立綁定新 HEAD、tree 與 request epoch 的 `review-requested` 狀態。舊 HEAD 的 Copilot review 不得採信或轉成新 HEAD 的 review；error、非 Copilot、非支援 state 或不同 commit 的 review 不得授權 ship。

### R4 不重送同一 HEAD stop

新 epoch 已對 candidate HEAD 建立後，相同 HEAD 的 stop 重播或重複 resume MUST NOT 再發出 Copilot request。若 request 的外部結果不確定，系統 MUST 記錄該 epoch 為 outcome-unknown 並停在 needs-human，不能以重送解決不確定性。

### R5 保留停止與審查歷史

切換 current `ship` 前，Manager MUST 將舊 timeout stop 與其 request/review 欄位 append 到該 run 的 delivery journal 歷史；既有 history 項目只能追加、不得改寫或刪除。新 epoch MUST 記錄其 run、HEAD、tree、authority digest、resume event 及 request 或 adopted review 身分，使新舊審查可分別追溯。

### R6 所有既有 ship gates 維持生效

恢復不得略過 exact-head CI/checks、mergeability、current review threads、ForeignReview、WorkAuthority、PR binding、tree、preflight、archive、completion 或 merge 前最終 HEAD 重讀。任何 new-head finding 經既有 fix-required 流程處理；current 未解 thread、未完成 CI、非 mergeable PR 或 HEAD race 都保持阻擋。

### R7 不自動建立 maintainer authority

此恢復不得產生 `review-attest`／maintainer-review evidence，也不得把 agent 或 resume 呼叫本身視為 review。既有 operator review-attest 的獨立契約不變，且不作為本票自動恢復的替代品。

## Observable outcomes

- 有效 new-head review 被採信時回傳該 HEAD 的正常 delivery 結果；沒有時回傳等待新 HEAD Copilot review。
- same-head stop、authority drift、binding mismatch、PR/tree race、未解 current thread、CI／mergeability gate 失敗及 request outcome unknown 都回傳 needs-human／具體原因，並保留可讀的歷史紀錄。
