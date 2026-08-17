# repo-root-fail-closed

- **`#612` repo root 的 cwd fallback 是危險預設：相對路徑輸入使 production 動作落在真實
  checkout**——`paths.repo_root()` 舊實作是
  `_resolve_root("PSC_REPO_ROOT", Path.cwd())`，而 manager daemon 的
  `WorkingDirectory` 正是 operator 的真實 cortex checkout。於是「解析不出目標 repo」
  不是失敗，而是**靜默落在錯的樹上**：`autonomy._infer_repo_root` 對**相對** spec 路徑
  的 `Path.resolve()` 接到 cwd，`paths.repo_root()` 的 cwd 預設又讓第一段
  `relative_to(configured)` 早退命中同一個 checkout。#565／#607 收掉了 `/tmp` 那半，
  cwd 這半留到本次。

  **完整危險呼叫路徑清單**（皆以 `_infer_repo_root` 或 `paths.repo_root()` 推出的
  `repo_root` 為 `git -C` 目標）：

  | 路徑 | 動作 | 語意 |
  | --- | --- | --- |
  | `manager.complete_tick → _completion_candidate_ref` | `git fetch --no-tags <remote> <branch>`、`rev-parse`、`merge-base --is-ancestor` | 讀（#610 實測打到真實 github.com） |
  | `autonomy.dispatch_ready → _resolve_target_base_sha` | 同上 fetch ＋ 相依 candidate 的 `merge-base` | 讀，且結果被 pin 進 job 的 `dispatch_base` |
  | `manager._resolve_ancestry_status` | `rev-parse` ＋ `merge-base --is-ancestor` | 讀，結果決定 slice 是否算 merged |
  | `manager.complete_tick → _candidate_for_evidence`（slice row 缺席時） | `git rev-parse <branch>` | 讀，**錯的 SHA 會被寫成 candidate 進 handoff manifest** |
  | `manager.apply_slice_action(recover-pre-candidate) → worktree_reclaim` | `git worktree list --porcelain` → `git worktree remove --force` → `git worktree prune` | **寫**——issue 預測的「同路徑家族若有寫入動作就是事故」實例 |
  | `seams.ScriptWorktreeCreator()`（`repo=None`） | `git worktree add`／`branch` 建立 | **寫** |
  | `manager` retry-verify／retry-review、`complete_tick` 的 dirty-worktree 重驗 | verification／review runner 的整組 git 操作 | 讀寫混合 |
  | `work_bridge.resolve_trusted_repo_root` | owner/name → repo 根解析把 cwd 收進候選 | 決定後續所有 lane 動作的目標 |
  | `deploy.installer.render_units` | 把 `<repo_root>` 寫進 systemd unit 的 `WorkingDirectory=` | 錯的目標會被**持久化**進 unit |

  **修法：fail-closed 取代 silent fallback。**

  - `config/paths.py`：新增 `RepoRootUnresolvedError` 與 `configured_repo_root()`
    （只回宣告值，未宣告回 `None`——讓「有沒有宣告」可被呼叫端分辨）。`repo_root()`
    改為 `repo_root(*, allow_cwd=False)`，未宣告 `PSC_REPO_ROOT` 時直接拋例外；
    `worktree_root()` 同步透傳。cwd 語意仍可用，但必須由呼叫端**顯式**表態——目前
    只有兩個 operator 手動 CLI 帶 `allow_cwd=True`：`cortex work gc`（`--repo-root`
    仍優先）與 `cortex deck compile`。
  - `coordinator/autonomy.py`：新增 `RepoRootResolutionError`（繼承 `ValueError`，
    沿用既有處置面），攜帶 #570／#527 的 `DiagnosticReason`。`_infer_repo_root`
    **拒收相對 spec 路徑**（`spec-path-not-absolute`），改讀 `configured_repo_root()`
    因此 cwd 不再經由「configured 預設值」偷渡，且向上找不到 git repo 根又未宣告
    `PSC_REPO_ROOT` 時 fail-closed（`repo-root-unresolved`）而非回 `spec.parent`——
    後者會被 `git` 自己向上走回一個被搜尋上界（`/tmp`）或名稱規則（`~/.agents`）
    刻意排除掉的 repo，等於繞過 #565 的保護。`parse_spec_frontmatter` 把該例外落成
    `parse_error`，掃描不中斷但該 spec 永遠停在 `hold`。
  - `coordinator/manager.py`：新增 `_repo_root_for_slice_row()` 單一推導點，移除三處
    `Path.cwd().resolve()` 退路；`_resolve_ancestry_status` 改回既有的
    `repo-unresolved` 狀態；`_repo_root_for_slice`（相依判定）推不出時回 `None`。
  - `coordinator/work_bridge.py`：候選只收顯式宣告的 repo 根。

  **不變式測試**：新增 `tests/test_repo_root_fail_closed_612.py`（13 測試）。每個測試
  都把 cwd 設成一個**真的** git repo（重演 daemon 在 operator checkout 裡跑的形狀），
  斷言 production 動作拒絕執行**且沒有任何 git 指令打向那個 cwd repo**——含
  `complete_tick` 對 #610 事故路徑的直接回歸（相對 spec path → 零 git 呼叫、零
  manifest、錯誤進 `errors`），以及三組對照組確認正常流程未被誤殺。

  **測試套件暴露出的非 hermetic 問題**（皆為「測試實際上在 operator 的真 repo 上動作」）：

  - `tests/conftest.py` 的 `_clear_runtime_env` 從未涵蓋 `PSC_REPO_ROOT`，於是全套
    3400+ 測試的 manager 目標 repo 一直是**跑 pytest 的當下目錄**。比照 #303 既有的
    `PSC_AGENTS_ROOT` 處理，改指向 per-test 暫存路徑。
  - `test_pre_candidate_recovery.py::test_recover_pre_candidate_supersedes_stale_handoff_manifest`
    與 `test_record_action_atomic_382.py::…test_recover_pre_candidate_cleanly_resets_failed_failed_slice`
    在真 checkout 上跑 `git worktree list --porcelain`（同函式再走一步就是
    `worktree remove --force`／`prune`）→ 改自備 tmp repo 並顯式宣告。
  - `test_porcelain_init_sample.py::test_init_sample_routes_before_coordinator_and_prints_hold_checklist`
    讀的是 operator 真 checkout 的 `.project-policy.yml`（測試綠不綠取決於在哪個目錄
    跑 pytest）→ 改自備最小 policy fixture。
  - `test_paths.py` 與 `test_fix_dispatch_spec_path.py` 的 cwd fallback 斷言改為
    fail-closed 斷言。
