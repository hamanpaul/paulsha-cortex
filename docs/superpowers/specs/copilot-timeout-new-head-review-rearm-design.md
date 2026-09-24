---
status: accepted
work_item: copilot-timeout-new-head-review-rearm
---

# Copilot timeout 新 HEAD 恢復設計

## Decisions

### D1 復原入口仍是 Manager control queue 的 resume

沿用 `execute_work_action(action="resume")` 的 operator 路徑；只在該路徑辨識 `copilot-review-timeout` 且舊 stop HEAD 不同於唯一 canonical ongoing run 的目前 candidate 時建立單次恢復許可。許可由 Manager 從 registry、WorkAuthority、journal 事實導出，不接受 caller 傳入的任意 HEAD、tree、review id 或 authority digest。`work_actions.py` 是現行 resume 與 `_ship_action` 共用的 Manager-owned mutation seam，避免增加 CLI/API 或直接編輯狀態。

### D2 journal 轉態採 run 與 state 條件比較

許可記錄 `run_id`、舊 stop 的 canonical hash、目前 `candidate_head`、WorkAuthority digest、PR binding hash、明示 action 的 `requested_by` actor 及 Manager 在 `_claim_action` 內產生的 transition id。`manager_daemon` 目前不把 control queue 的 `req_id` 傳至此層，所以 transition id 僅識別本地轉態，不聲稱是原始 queue request id。寫入前在 daemon 單 writer／flock 序列化範圍內，再次比較讀取的 journal `ship` 與所選 canonical run；任何可觀測的 stale state 或多個 ongoing run 都拒絕。Ship validator 重新讀取全部 authority／remote facts，只有 tuple 完整相等才可消費許可。重複 resume 對同一 tuple 冪等；不同 tuple 或已消費的 transition 不得覆寫舊 history。這是單 writer 條件比較，不宣稱跨 writer 的原子 CAS。

### D3 擴大檢查順序只限舊 HEAD Copilot timeout

目前 `_ship_action` 對一般 `needs_human` stop 會在 candidate、preflight、PR HEAD 比對前立即返回。保留這個預設；只有符合 R1 的舊 HEAD timeout 才延後判定，先走既有 candidate、preflight、delivery binding、PR facts 與 tree 驗證。不得讓其他 `copilot-*`、maintainer、merge、preflight 或 correlation stop 進入此路徑。

### D4 先保存 epoch 歷史，再開始新 request

在 run row 中追加舊 ship snapshot 至 append-only `delivery_review_epochs`，並把恢復許可／transition id 綁入新 epoch。已有項目需逐項比對 canonical hash，唯有原有 list 為新 list 的不變 prefix 才可追加。外部 request 前先持久化 `review-requesting` 與 request identity；成功後才轉 `review-requested`。若程序在外部副作用前後中止，重播先查有效 exact-head review；無法證明 request 結果時轉 `copilot-review-request-outcome-unknown`，不重發 request。

### D5 複用 #948 adoption 與現有 delivery evaluator

新 HEAD 的有效 review 走現有 Copilot author/state/error/commit 篩選與 `ReviewLoop`／delivery evaluator；沒有有效 review 才 request。finding、thread、checks、mergeability 與 final HEAD recheck 照舊執行。request/adoption 都必須寫出新 epoch 欄位，舊 review id 不能隨新 `ship` 狀態沿用。

### D6 不新增 review authority 或一般 recovery API

不建立 maintainer attestation、不改 `review-attest`、merge authorization schema、reviewer identity、 timeout 長度、finding budget 或其它 stop 的 recovery allowlist。此票只增加 Manager 控制下的 timeout 舊 HEAD → 新 exact-head 恢復狀態。

## Failure handling

- 新 HEAD／tree／authority／binding 在任一重讀點不同：不 request、不採信 review、不 merge；舊 timeout snapshot 保留，許可失效，回報 first mismatch。
- 未解 current thread、required check 未完成／失敗、PR 非 mergeable：新 review 不能授權 merge，沿用既有阻擋 reason。
- request 呼叫的回應不確定：以 durable outcome-unknown 停止；不自動 retry，也不聲稱 Copilot 已完成 review。
- malformed ship state、history hash 重複但內容不同、或 run identity 不唯一：fail closed，不修補既有 row。

## Risk

此狀態機只有在 Manager queue 串行化 `resume` 與 ship mutation 的前提下才構成單一 writer；`_save_runs()` 是 atomic replace，沒有跨 writer CAS。並行 Manager writer／journal 外部修改不受本票保護，遇到可觀測的非預期 revision 必須停止；若驗收證明需要跨 writer 原子性，應先交由既有 #966／#983 的 registry／journal CAS 工作處理，不可在本票內假稱已保證。GitHub 的 PR review request API 不能原子綁定 SHA；因此需在副作用前後重讀 PR HEAD，外部 request 若撞上 push race 只能記錄為未授權的 outcome-unknown，不能用該 review 走過 gate。
