# publish-state-transaction

- **`#536`（後半）：planning artifacts 發佈與 run 狀態更新收進同一個 crash-safe 事務，
  並補上唯一的恢復路徑**——define 的 brainstorm 有兩次分離的 durable 寫入：先把
  spec/design/plan 發佈到 operator worktree（`_publish_planning_artifacts` →
  `_PlanningPublicationTransaction`），再把 run 推進到 `plan` 並寫入 gate_refs／
  planning_authority（`registry._manager_update_workflow_run`）。兩者之間崩潰就留下
  「artifacts 已落地、run 狀態停在原地」的中間態，正是 #536 現場。

  journal（`<coordinator_root>/planning-transactions/<run_id>.json`）本來就記了每個
  mutation 的 before/after hash，缺的是**誰去看它**：`reconcile()` 只能由持有該 run
  的呼叫端逐 run 觸發（define 起始、`resume_workflow_run`），run 一旦離開 `ongoing`
  （superseded／done）就再也沒有任何迴圈會碰它。實測 coordinator root 上躺著兩份這種
  孤兒 journal，其中一份正是 #536 現場的 `workflow-7a430d31eff66ef13630`——run 已被
  abandon 成 `superseded`，兩份 `before_exists=false` 的 spec/design 殘留檔就此永久
  留在 operator worktree，成為下一世代 define 撞 #416／#535 authority fail-closed 的
  地雷。另一份 `workflow-29a1247ddaf88d11eda8` 同型（8/11 起）。

  修法三項：
  1. **`prepare_commit()` 把事務邊界寫成 durable 事實**——journal schema 升到 v3 並新增
     `phase`（`publishing` → `prepared`）。`prepared` 表示檔案側已全部落地且 fsync，
     下一步就是本事務唯一的 commit point（registry 的原子寫入）；封住之後再呼叫
     `publish()` 一律 fail-closed。`phase` 只供診斷，**不參與** commit 判準。
  2. **`reconcile_planning_transactions()`：掃整個 journal 目錄的唯一恢復路徑**——與
     run 狀態無關，ongoing／superseded／done 一視同仁，因此既有殘留與未來崩潰走同一條
     程式路徑自癒。判準只有一條：**registry 的 run row 上有沒有這次的 brainstorm
     gate ref**。有 → 前滾（逐位元組驗證每個已提交產物後退役 journal）；沒有 → 回退。
     選回退而非前滾的理由：沒有那個 gate ref 就代表這批產出從未被綁進任何 run（無
     authority、無 source revision、steps 沒有 outputs），前滾在語意上不成立；而
     現場殘留的 `expected_gate_ref` 是 `null`（崩在 evidence 落地之前），根本沒有可以
     前滾的目標。回退全程受既有 CAS 護欄把關（`before_exists` ＋ `after_hash` 必須
     完全吻合），撞到 operator drift 一律拒絕並改為呈現。
  3. **收斂結果不得靜默**——每份 journal 的處理結果（`rolled-back`／`committed`／
     `adopted`／`drift`／`unknown-run`／`in-flight`）都落結構化 log 並進 tick summary；
     無法自動收斂（drift）且 run 仍 `ongoing` 時補 `needs_human` facet，讓
     `cortex status` 的 attention 清單有話說。

  安全護欄：(a) journal 寫下未滿 5 分鐘者視為「可能還在飛」，本輪不碰——發佈側最後
  一次 fsync 到 registry 提交正常是次秒級，這個餘裕純粹是為了絕不誤傷 daemon 之外
  的前景發佈；(b) 殘留檔若已被 operator 納入 git 追蹤，就不再是「未提交的發佈殘留」，
  跳過刪除並回報 `adopted`（比照 `work_actions._gc_one_abandoned_planning_artifact`
  的既有紀律，#507 的教訓）；(c) 找不到對應 run row 時 fail closed——無法驗證 journal
  自報的 workspace root 就不刪檔，只留 log 與回報；(d) sweep 整批失效比照 #246 的 tick
  isolation 降級，不得癱瘓本輪 tick。

  恢復路徑相容 v2 journal（升級前的格式，實際部署上就有殘留），自癒不要求 operator
  先手動搬遷。不動 #538 已修的 resume 迴圈 phase filter；phase 心跳仍屬 R0.5 D5。
  新增 `tests/test_planning_publication_transaction_536.py`（14 個回歸測試，含「發佈後、
  狀態更新前行程當場死亡」的端到端邊界測試與既有 v2 殘留自癒測試）。
