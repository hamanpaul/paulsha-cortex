# rollback-containment

- **`#507`：planning 失敗時整棵 operator worktree 抹除還原，會靜默銷毀並行的 operator 工作
  （實測資料遺失）**——`planning_runtime._invoke_json` 的 finally 區塊只要偵測到 T0→T1 之間
  operator worktree 有任何差異，就呼叫 `_restore_operator_tree()`：刪光 worktree 內除 `.git`
  以外的**全部內容**，再從 T0 baseline 複本整棵還原。偵測條件（`_tree_snapshot` 前後比對）
  完全無法分辨「launcher 越界寫入」與「operator／其他 agent／編輯器在同一時間的正常編輯」，
  而 launcher 本身早已以 `cwd=sandbox`（拋棄式複本）執行——安全網的補救動作被設成整棵樹抹除，
  誤傷機率遠高於它要防的越界。加上 baseline 由非原子 `copytree` 取樣，歸因本身就不可靠。
  Phase 1 dogfooding 兩次實測命中：
  - run `workflow-0529388d8e290c8fb938`：operator 在 planning 視窗內新建的
    `docs/superpowers/workstreams/<slug>/todo.md` 被抹除。連帶後果不只丟檔——`cortex work link`
    已把該 todo 寫進 `.cortex/work-items.yaml` 成為 path source，檔案消失後 registry 留下懸空
    連結、`active_todo` 為假、lifecycle 停在 `topic` → 不可 claim。
  - 更嚴重的形態：**被抹除的是 cortex 自己的成功產出**。前一代 planning 產出的三份合格
    artifact 是未追蹤檔、不在後續那次的 T0 baseline 內，被下一次失敗的 rollback 當成 launcher
    產物刪除；run 的 `planning_authority` 隨即指向不存在的檔案
    （`manager._workflow_input_snapshot` → `workflow planning input missing`），work item 卡死，
    且 ship 前皆未追蹤、git 救不回。
- **R0 修法（範圍收斂 ＋ 備份 ＋ 報告，不改結構）**：
  - **整棵還原的程式路徑移除**：`_restore_operator_tree()` 刪除，改由
    `_contain_operator_drift()` 承接。`_make_tree_traversable()` 收斂為只能指向拋棄式
    sandbox——它把整棵樹的目錄 mode 強制改成 `0o700`，本身就是一次寫入，過去靠事後整棵還原
    蓋回去才成立。
  - **預設不改寫 operator worktree 一個位元組**：drift 分析改走全新的唯讀
    `_tree_manifest()`／`_diff_tree_manifests()`，對讀取失敗容錯（被 chmod 0 的角落記成
    `unreadable` 繼續，不再為了讀取而 chmod 整棵樹）。偵測到 drift 仍 fail-closed，但處置權
    交回 operator。
  - **受影響檔案完整備份進 run-scoped evidence**：
    `<coordinator_root>/evidence/planning-worktree-drift/<run_id>-<digest>/` 下同時保存
    `observed/`（T1，也就是萬一被抹除就永久消失的那一版）與 `baseline/`（T0），並落一份
    `cortex-planning-worktree-drift/v1` 結構化 diff 報告（逐路徑 change／mode／sha256／
    symlink target／xattr 摘要／備份落點）。失敗訊息帶上計數與 evidence 路徑，另落一筆
    `logger.error` 保全完整訊息（比照 `#511`）。
  - **還原改為需明示 opt-in 且逐路徑收斂**：`rollback_scope` 預設空集合；`_invoke_json` 明確
    傳空集合——launcher 以 `cwd=sandbox` 執行，這條路徑在 operator 樹裡沒有任何「本次 run
    自產」的產物可言，可證明的還原範圍就是空的。三道 fail-closed 閘門：不在本次 diff 內、
    命中受保護的權威文件（`docs/superpowers/{workstreams,specs,plans}/**`、
    `openspec/changes/**`、`.cortex/**`）、備份未成功者，一律拒絕還原——**備份不成功就不准
    抹除**是本 issue 最低限度的保命索。
  - `manager.apply_workflow_action` 把 `evidence_root`（transaction root）與 `run_id` 交給
    runtime factory，operator 因此能用同一組 run_id 同時撈 `planning-recovery`、
    `planning-artifacts`、`planning-worktree-drift` 三份 evidence。未帶入時**不寫任何
    evidence**，刻意不 fallback 到 `paths.coordinator_root()`，避免非 daemon 呼叫端在
    operator 執行期狀態目錄留下非預期檔案。
- 結構解（planning 產出完全不進 operator 樹）屬 R2 evidence 模型範疇，不在本次範圍；
  baseline 取樣的非原子 race、planning 期間的 advisory lock、以及 worktree drift 仍被分類為
  `content`（`recover-planning` 因此禁用）同樣留待後續。
