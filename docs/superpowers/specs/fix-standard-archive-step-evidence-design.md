---
status: accepted
work_item: fix-standard-archive-step-evidence
---

# fix-standard 以 Manager archive job 證據判定 archive-applied 與 archive 結果 fail-closed 設計

## Decisions

### D1 宣告 step 優先、未宣告以 job 證據——不在 registry 插入 step

`manager._manager_archive_applied(run, *, registry=None)` 先找 `run.steps` 中 `phase == "ship" and card == "openspec-archive"` 的 step：有 → 維持現行判式（passed 恰好一筆且 `(executor, model, domain) == ("cortex-manager", "deterministic", "cortex")`），`registry` 忽略；沒有 → `registry is None` 回 False，否則回 `_manager_archive_job_applied(registry, run)`。

不採 issue 提議的另一條路「`_manager_reset_workflow_after_archive` 缺 step 時插入 passed step」：`manager.py:4189-4200`（689ad7b1）已明定「`fix-standard` deliberately omits the Manager-only ship cards from `run.steps`」並讓 `_review_builder_job_binding` 以 archive job 的 ship evidence 採信；插入 step 會讓 persisted `steps` 與 compile 出的 manifest 分岔、多改 `registry.py` 並影響 status 投影。沿用同一先例，archive 是否完成的 authority 在未宣告卡的 combo 上就是 Manager 自己寫下的 archive job。

### D2 job 證據判準

新增 `manager._manager_archive_job_applied(registry, run) -> bool`：

1. `candidate = run.candidate_head`，不符 `verification.SAFE_SHA_RE` → False。
2. 取 `registry.list_jobs()` 中 `workflow_run_id == run.run_id`、`workflow_phase == "ship"`、`workflow_card == "openspec-archive"`、`persona == "manager"`、`executor == "cortex-manager"`、`model_id == "deterministic"`、`independence_domain == "cortex"`、`status == "exited"`、`exit_code == 0`、`isinstance(workflow_evidence, dict) and workflow_evidence.get("kind") == "ship"` 的 job；這組欄位正是 `work_bridge._record_manager_ship_job` 寫入並經 `bind_workflow_evidence` 綁定的形狀，也與 `work_bridge._builder_binding` 的 `manager_archive` 判式一致。
3. 筆數 != 1 → False（對齊 step 判式「crash／retry 多筆視為未完成」的 fail-closed 語意）。
4. `subject_head` 不符 SAFE_SHA → False；`subject_head == candidate` → True；否則 `subprocess.run(["git", "-C", str(run.workspace_root), "merge-base", "--is-ancestor", subject, candidate], shell=False, capture_output=True, text=True, check=False)`，`returncode == 0` → True，其餘或 `OSError` → False。形狀逐字沿用 `_validated_ship_steps.matches_candidate` 的 ancestry 呼叫。

不驗 claim era／`source_revision`：#765 已裁定 build／archive 產物跨 authority era 保留，`_review_builder_job_binding` 同樣不驗。archive commit 由 #649 harvest 進來源樹，`run.workspace_root` 上一定有它；沒有時 git 回 128 → False（fail-closed）。

### D3 消費端接線

- `work_bridge._manager_archive_applied(run, *, registry=None)` 委派 `manager._manager_archive_applied(run, registry=registry)`；既有兩條 parity 測試（無 registry）答案不變。
- ship 守衛（`build_production_ship_validator.validate`）在計算 `active_change` 後呼叫一次 `archive_applied = _manager_archive_applied(run, registry=registry)`，供 D5 與原 archive 分支共用。
- `_validated_ship_steps.matches_candidate` 改呼叫 `_manager_archive_applied(run, registry=registry)`；其餘（exact-one job、evidence payload 驗證、`_audit_phase_steps`）不動。fix-standard run 無 `openspec-archive` step，`_audit_phase_steps(card_id="openspec-archive")` 維持 no-op。
- `work_actions._retry_build_action` 以 lazy import（比照 `_phase_recovery_actions` 對 `_current_workflow_step` 的作法）或經 `work_bridge._manager_archive_applied` 呼叫同一實作並帶 `registry=workflow_registry`，刪除 inline `any(...)`。對宣告 step 的 combo，舊 `any` 與新判式只在「多筆或 identity 不符的 passed ship step」上不同，而那些狀態本來就被 `registry._manager_reset_workflow_for_retry_build` 以 `Manager-owned archive authority` 拒絕，受理路徑上等價。
- `work_actions._retry_build_action` 對 `len(authority.mapped_openspec) == 1` 的 run，在呼叫 `_manager_reset_workflow_for_retry_build` 派 builder 前，以 `run.workspace_root` 上 `run.candidate_head` 的 Git tree（不是來源 checkout 的工作目錄、builder clone 或未提交檔案）檢查唯一 mapped change：是否同時有 `openspec/changes/<change>/...` 與 `openspec/changes/archive/<entry>/...`，其中 `<entry> == <change>` 或以 `-<change>` 結尾。命中時 retry-build 照常派工、不拒絕唯一修復出口，並在 action 回傳新增 `warnings` 項目，明確列出 change 與全部 matching archive entries，提醒本次 exact Candidate 含 active/archive 並存、須在下一次 review 前修正。此為實際 tree 檢查後的 operator 可見 warning，不以 builder 指示代替檢查；mapped change 數不是 1 時此 OpenSpec 檢查不適用，既有 retry-build 行為不變。若適用檢查無法讀取／解析 exact Candidate tree，raise 明確 `RuntimeError` 並在派工前停止，不得當作沒有並存而靜默續跑。
- post-archive 分支文字尾端（`Commit or adopt a tested descendant Candidate.` 之前）插入一句：`If the Candidate already contains a re-created active change directory for the archived change under openspec/changes/ (outside openspec/changes/archive/), delete it and keep only the official archive.`；其餘句子逐字保留。這是已確認 archive-applied 時對實際 warning 的修復指示，不取代上述檢查／回報。
- `_planning_artifact_relative_path_after_archive` 兩個呼叫端拿不到 registry，維持 `_manager_archive_applied(run)`。`work_bridge._artifact_rows`（`work_bridge.py:241-245`）雖把 `openspec/changes/<change>/{proposal,design,tasks}.md` 種進 planning authority，但 fix-standard post-archive 沒有路徑會解析它們：`_workflow_input_snapshot`（`manager.py:6118`）只為 declared input 解析 authority ref，而 fix-standard compile 後 build 卡 inputs 只有 `docs/superpowers/plans/*<slug>*.md`、verify／review／`policy-commit` 無 inputs；`_validated_brainstorm_planning_authority`（`manager.py:3401`）只逐檔解析 brainstorm evidence 的 artifacts，其 destinations 由 `planning_runtime._planning_destinations` 固定在 `docs/superpowers/`。

### D4 archive 結果兩道檢查

- **Aborted（R4）**：ship 守衛在既有 `returncode != 0` 檢查之後，以 `output = f"{getattr(archived, 'stdout', '') or ''}{getattr(archived, 'stderr', '') or ''}"`；`"Aborted" in output` → `raise RuntimeError("official OpenSpec archive aborted: no files were changed")`。放在 `_commit_archive_and_require_reverification` 之前，scaffold 殘留留在 ship 工作區，下一 tick 由 `_reset_ship_workspace` 打回 pristine。
- **搬移後置條件（R5）**：`_commit_archive_and_require_reverification` 在 `changed` allowlist 檢查通過後、`git add` 前：`active = worktree / "openspec" / "changes" / change`，`active.is_dir()` → raise；`relocated = any(parts[:3] == ("openspec", "changes", "archive") and len(parts) > 4 and (parts[3] == change or parts[3].endswith(f"-{change}")) for parts in (Path(p).parts for p in changed))`，False → raise；訊息皆含 `official OpenSpec archive relocation missing`。檢查位於 harvest／`_record_manager_ship_job`／`_manager_reset_workflow_after_archive` 之前，因此失敗時 registry 與來源樹零副作用。`<entry> == <change>` 的容忍是為既有測試 fixture（`archive/work`）；官方 CLI 產出 `YYYY-MM-DD-<change>`。
- `work_actions._ship_action` 直接路徑不加檢查：它不 commit、回 `archive-applied-needs-commit` 後由 work_bridge 的 `_commit_archive_and_require_reverification` 收尾，R5 已涵蓋；且 `tests/test_work_actions.py` 三條 `archive-applied-needs-commit` 測試的 runner 不搬檔，改那條路徑會牽動無關斷言。

### D5 active 與 archive 並存 fail-closed

ship 守衛新增一道：`active_change is not None and active_change.is_dir() and archive_applied` 且 `worktree/openspec/changes/archive` 是非 symlink 目錄、其下有非 symlink 子目錄名稱 `== change` 或 `endswith(f"-{change}")` → `raise RuntimeError("post-archive candidate re-created active OpenSpec change alongside its official archive")`。例外經 `resume_workflow_run` 的 review→ship advance 包成 `review-advance-failed` needs_human（既有路徑），operator 以 `retry-build` 重開，R3 文字指示 builder 刪除重建的 active 目錄。`archive_applied and active 存在 and 無 archive entry` 不攔，保住 `tests/test_ship_out_of_builder_clone_653.py::test_archive_applied_needs_commit_reentry_commits_in_the_manager_tree` 釘住的重入契約。

並存候選也可能先被 reviewer 判 fail（#885 的 486 判 scope-bypass，當時候選多了 Manager 用 scaffold 做出的第二筆 archive commit；修正後 R1 讓 ship 守衛不再重跑 archive，那種 commit 不會再出現），此時 ship 守衛尚未執行。D3 的 retry-build exact-tree 檢查在下次 builder 派工前直接回報這個候選狀態；D5 保留為之後 review→ship advance 的 fail-closed 防線。retry-build 遇到已確認的並存仍須派 builder，因為它是唯一修復出口；不得把 D5 當成滿足 issue 對 retry-build warning 的要求。

### D6 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| predicate：fix-standard | `compile_combo(load_combo(DEFAULT_COMBOS_DIR / "fix-standard.yaml", cards), cards, "t", change="work", allow_external=True).workflow_manifest.steps`；`JobRegistry`＋`work_bridge._record_manager_ship_job(card="openspec-archive")` | subject == candidate → True；真 git repo 後代 → True；sibling（`git commit-tree` 同 tree 另一 parent）→ False；兩筆 job → False；`registry=None` → False |
| predicate：identity／evidence | 直接 `registry.create_job` 造 executor／model_id／independence_domain／persona 不符或未 `bind_workflow_evidence` 的 job | 全部 False |
| predicate：宣告 step 優先 | feature-oneshot manifest（step pending）＋registry 有 archive job | False；既有 `test_manager_archive_applied_semantics_match_between_manager_and_work_bridge` 參數全綠 |
| ship audit 後代 | 比照 `test_ship_audit_accepts_manager_archive_ancestor_after_retry_build`，steps 換 fix-standard manifest | `_validated_ship_steps` 通過；unrelated sibling → `missing or ambiguous: openspec-archive` |
| retry-build 並存偵測／warning | 建立 exact candidate commit tree 同時含 `openspec/changes/work/tasks.md` 與 `openspec/changes/archive/2026-09-14-work/spec.md`，另讓來源 checkout 有不同工作目錄狀態；archive job 指向 candidate | 派工前從 candidate tree 偵測並存；仍回 `candidate-repair-dispatched`，回應 `warnings` 明列 active/archive change；post-archive action 含兩個既有子字串＋`re-created active change directory`、不含 `pre-archive work`；未提交工作目錄不影響結果 |
| retry-build 無法檢查 tree | exact candidate SHA 合法但 `git ls-tree` 執行失敗／輸出格式無法解析 | 派工前 raise 明確 `retry-build ... candidate tree ...`；不得靜默派工並讓下一次 review 才發現 |
| Aborted | `test_preflight_closeout_order._ship_harness(active_change=True, archived_change=False)`，包裝 runner 分別以 stdout／stderr 回 `returncode=0`、`Aborted. No files were changed.`、不搬檔 | 兩種輸出位置都 raise 含 `official OpenSpec archive aborted`；`candidate_head` 不變；無 `workflow_card == "openspec-archive"` job；`job_workspace.source_branch_head(repo, "feature/14-work") == candidate`；`returncode != 0` 仍維持既有 fail |
| 未搬移 | 同上 stdout 空 | raise 含 `official OpenSpec archive relocation missing`；同上三條不變式 |
| ship 並存 backstop | `monkeypatch.setattr(test_preflight_closeout_order, "_repo", <init commit 同時含 openspec/changes/work/ 與 openspec/changes/archive/2026-09-14-work/ 的變體>)` 後建 harness，再以 `_record_manager_ship_job(new_head=candidate)` 造 archive job | ship validator raise 含 `re-created active OpenSpec change`；runner 呼叫紀錄無 `["openspec", "archive", ...]`；retry-build warning 測試另證明這不是唯一偵測點 |
| 既有契約 | `tests/test_work_bridge.py`、`tests/test_preflight_closeout_order.py`、`tests/test_ship_out_of_builder_clone_653.py`、`tests/test_ship_phase_harvest_649.py`、`tests/test_workflow_production_wiring.py`、`tests/test_work_actions.py`、`tests/test_ship_lane_no_openspec_911.py`、`tests/test_reviewer_candidate_tree_650.py` | 全綠、不改斷言 |

### D7 Sizing

3 個 production 模組（`manager.py`、`work_bridge.py`、`work_actions.py`）→ `domain_breadth=1`；只讀既有 durable job 列與 exact Candidate Git tree，不新增 persisted 欄位或 CAS，新增的 archive commit／harvest 檢查仍在副作用前 fail-closed；retry-build warning 只增加 response evidence、維持既有派工狀態 → `state_consistency=1`。invariant_count 從 6 增為 7（另含 retry-build 對並存候選的派工前偵測與 operator warning），但此數不改五維 sizing 計算。沿用 fix-standard 既有 projection：`domain_breadth=1 + state_consistency=1 + acceptance_surfaces=2 + spec_stability=0 + orchestration=2 = 6`，為 Yellow（Yellow 上限 6）；full #885 scope 仍可同一票完成，無須另拆 issue 或下修要求。
