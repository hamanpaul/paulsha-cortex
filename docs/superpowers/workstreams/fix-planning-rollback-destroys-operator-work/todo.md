---
status: accepted
work_item: fix-planning-rollback-destroys-operator-work
---

# fix-planning-rollback-destroys-operator-work Todo

`#507`：planning 階段偵測到 operator worktree 有變動時，`_restore_operator_tree()` 會
**刪除該 worktree 內除 `.git` 以外的全部內容**，再從啟動當下（T0）的 baseline 複本還原。
未追蹤檔不在 git 內，**不可復原**。

## 根因（`coordinator/planning_runtime.py`）

- `:375-378` — `operator_before = _tree_snapshot(worktree)` 與 `_copy_planning_sandbox(worktree, baseline)` 都在 T0 取樣。
- `:423-437` — launcher 結束後（T1）以 `_tree_snapshot(worktree) != operator_before` 判定 `operator_dirty`，為真即還原。
- `:205-224` — `_restore_operator_tree()` 逐一 `child.unlink()`／`shutil.rmtree(child)`（僅跳過 `.git`），再由 baseline 複製回來。

亦即 **T0→T1 之間的任何差異都被歸因為 launcher 汙染**。但 launcher 本身已以
`cwd=str(sandbox)`（可拋棄複本）執行，operator worktree 檢查本質上只是「防越界」的
安全網；把安全網的補救動作設成整棵樹抹除還原，在多方並行（operator 手動編輯、其他
agent、編輯器自動儲存、背景建置）的真實環境下，誤傷機率遠高於真正的越界。

## 實測命中（Phase 1 dogfooding，兩次）

1. **operator 工作被抹除**：run `workflow-0529388d8e290c8fb938`（`fix-rate-limit-classification`／
   phase `define`）失敗時，抹掉 operator 在該視窗內新建的
   `docs/superpowers/workstreams/fix-read-repo-tier-fail-closed/todo.md`。視窗**之前**已存在的
   修改因已納入 baseline 而倖存，正好佐證「還原到 T0 baseline」的語意。
   連帶後果：`cortex work link` 已把該 todo.md 寫進 `.cortex/work-items.yaml` 成為 path source，
   檔案消失後 registry 留下懸空連結，`active_todo` 為假 → lifecycle 停在 `topic` → 不可 claim。
2. **cortex 抹除自己的成功產出（嚴重度升級）**：`#514` 那一輪 planning 產出的三份合格 artifact
   （前一代產生的未追蹤檔、不在後續那次的 baseline 內）被下一次失敗的 rollback 抹除，
   導致 `workflow planning input missing`（`manager.py:3971`）、work item 卡死。
   自外部備份還原後四個 `baseline_sha256` **全部 MATCH**，證實內容原本合法。
   **含意：成功本身會被系統自己銷毀，且 git 救不回（ship 前皆未追蹤）。**

## 第三則缺陷：非原子快照 race

`_copy_planning_sandbox`（`:152-177`）以 `copytree` 取 baseline，過程非原子；在複製途中發生的
operator 寫入可能落在 baseline 內或外，取決於遍歷順序——同一次編輯的多個檔案可能被拆成
「一半保留、一半抹除」的不一致狀態。

## 第四則缺陷：content 誤分類導致確定性死鎖

worktree 汙染被分類為 `content`（`secondary-output-malformed: ... modified operator worktree`），
而 `work_actions.py:2912` 依設計（`#393`）禁止 `content` 類失敗使用 `recover-planning`。
於是這條路徑成為**永久死路**：唯一出口是 abandon，而 abandon 後重試會遇到完全相同的失敗。

## Tasks

- [ ] **預設不得改寫 operator worktree**：偵測到 dirty 時讓 planning run 失敗並在 evidence 報告 diff（路徑清單＋雜湊），處置權交回 operator；還原行為改為需明示 opt-in
- [ ] **抹除前必須留下可復原備份**：若仍執行還原，先把當下 worktree 狀態存入 evidence 目錄（或 `git stash create` 等可回溯物件），並在錯誤訊息指明復原路徑
- [ ] **縮小歸因範圍**：只還原 launcher 能證明寫過的路徑（provenance／fs 事件），而非整棵樹；至少排除 `.gitignore` 內容與已知 operator 產物（`.venv`、`build/`、`*.egg-info`）
- [ ] **保護 cortex 自身的 planning 產出**：`docs/superpowers/{specs,plans}/**` 下由前代 run 產生的 artifact 不得被後續 rollback 抹除（否則 `manager.py:3971` 會把 work item 鎖死）
- [ ] **baseline 取樣需一致**：非原子 `copytree` 的 race 需消除或明確界定（例如快照前後比對、或以 git 物件而非檔案樹為 baseline）
- [ ] **並行保護**：planning 期間對該 repo 取 advisory lock 或記錄「planning 進行中」狀態供 operator／其他 agent 查詢
- [ ] **失敗分類修正**：diff 明顯屬 operator 語意（tracked 檔的正常編輯、與 launcher 無 provenance 關聯的新檔）時分類為 `operator-concurrent-edit` 而非 launcher 汙染，且該分類**必須有非 abandon 的復原出口**（現況落入 `#393` 的 content 禁令而成死路）
- [ ] **測試**：涵蓋「planning 期間第三方新增未追蹤檔」「planning 期間修改既有 tracked 檔」「前代 planning artifact 存在時發生 rollback」三情境，斷言內容不被銷毀且失敗訊息可辨識、可復原
