---
status: accepted
work_item: fix-standard-archive-step-evidence
domain_breadth: 1
state_consistency: 1
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# fix-standard 以 Manager archive job 證據判定 archive-applied 與 archive 結果 fail-closed（#885）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#885`；[spec](../../specs/fix-standard-archive-step-evidence-spec.md)、[design](../../specs/fix-standard-archive-step-evidence-design.md)。
- 觸及模組（3 個 production 模組 → `domain_breadth: 1`）：`paulsha_cortex/coordinator/manager.py`（`_manager_archive_applied` 加 `registry` 分支、新增 `_manager_archive_job_applied`、`_validated_ship_steps` 帶 registry）、`paulsha_cortex/coordinator/work_bridge.py`（`_manager_archive_applied` wrapper、`build_production_ship_validator` ship 守衛的 Aborted／並存 backstop、`_commit_archive_and_require_reverification` 搬移後置條件）、`paulsha_cortex/coordinator/work_actions.py`（`_retry_build_action` 的 archive-applied 判定、exact candidate tree 並存偵測／warning 與 post-archive 文字）。`state_consistency: 1`：只讀既有 durable job 列、Git ancestry 與 exact candidate tree；warning 不新增 persisted 欄位／CAS；archive commit／harvest 檢查仍在副作用前 fail-closed。
- 明確不做：不改 `registry.py`（不在 `_manager_reset_workflow_after_archive` 插入 step）、不改 combo YAML／deck compile、不改 `_planning_artifact_relative_path_after_archive`（fix-standard post-archive 無消費端解析 openspec planning ref，見 design D3）、不改 `work_actions._ship_action` 直接路徑的 archive 呼叫、不拒絕已偵測到並存候選的 `retry-build`（它是唯一修復出口；R3 派工並回報 warning，R6 作 ship 段 backstop，見 design D5）、不修 pre-fix 殘留兩筆 archive job 的 run；不處理 #876、#953、#887、#808、#847。
- spec／design／本 todo 文字是 pinned authority：只准把 `[ ]` 翻成 `[x]`，不得改寫任何其他文字；澄清寫進 terminal reason。
- 留在 Manager checkout 的分支上工作，不得另建 `wt/...` 分支。
- Cortex intake 若產生 `docs/superpowers/plans/fix-standard-archive-step-evidence.md`，該 Manager 工作檔不得由產品實作 PR commit 或刪除；進件時 repo 不要求預先存在此檔。
- tests 與 docs 不得 hard-code `openspec/changes/<change>/` 路徑（本 work item 自己的 active change 會被 Manager archive 搬走）；測試在 `tmp_path` 內自建的 fixture change（如既有 harness 的 `work`）不在此限。

## 現場證據

- 2026-09-14 run `workflow-ced42c7b999df8bc222d`：fix-standard＋openspec，archive commit 09d6ed0c 後 retry-build 三次；4d473fcf 依 pre-archive repair 文字重建 active `tasks.md`；ship 再 archive 得 `Aborted. No files were changed`（exit 0），Manager 仍把 scaffold 收成 ac091a27（job 484）→ 重審 486 scope-bypass；兩條出口（保留 → `archive diff escaped strict allowlist`、刪 active → `ship card audit missing or ambiguous: openspec-archive`）皆不通，operator 場外 retry-build 重做乾淨 archive。
- 現行 main `7fa4716b`：`fix-standard.yaml` compile 後 ship 只有 `policy-commit`；`manager.py:3236-3248` 只認 passed step；`registry.py:2153-2198` 只標既有 step；`manager.py:4189-4200` 已對 fix-standard 以 archive job 證據採信 review builder（689ad7b1 先例）；`manager.py:7855-7881` ancestry 分支對 fix-standard 恆關；`work_bridge.py:1920`／`1936-1944` 只看 returncode；`work_bridge.py:1319-1408` 只驗 allowlist 非空；`work_actions.py:2466-2495` inline step 判定使 fix-standard 走 pre-archive 文字。

## Tasks

- [x] **T1 tests／RED**：新增 `tests/test_fix_standard_archive_step_evidence_885.py`，逐條對應 spec R7 (a)–(g)。沿用 `tests/test_preflight_closeout_order.py` 的 `_ship_harness`／`SpyRunner`／`_repo`／`_capture`、`tests/test_workflow_production_wiring.py::test_ship_audit_accepts_manager_archive_ancestor_after_retry_build`、`tests/test_work_actions.py::test_retry_build_preserves_only_manager_owned_archive_authority` 樣板；fix-standard steps 以 `compile_combo(..., allow_external=True)` 取得（見 design D6）。現行必須 RED：(a)(b) fix-standard 後代判 False／`missing or ambiguous: openspec-archive`；(c)(d) retry-build 沒有 exact-tree warning／未提交工作目錄負例不受控；(e)(f) scaffold 被 commit 成新 candidate；(g) ship 並存未 raise。
- [x] **T2 source／archive-applied 判準（R1、D1、D2）**：`manager._manager_archive_applied(run, *, registry=None)`。宣告 `openspec-archive` step → 現行判式逐字不變；未宣告且 `registry is None` → False；未宣告且有 registry → 新增 `_manager_archive_job_applied(registry, run)`：身分／終局／`workflow_evidence.kind == "ship"` 全符的 Manager archive job 恰好一筆，且 `subject_head` 等於 `run.candidate_head` 或經 `git merge-base --is-ancestor`（`run.workspace_root`）證實為祖先；其餘一律 False。
- [x] **T3 source／消費端接線與 repair 文字（R2、R3、D3）**：`work_bridge._manager_archive_applied` 簽名加 `registry=None` 並委派。ship 守衛與 `_validated_ship_steps.matches_candidate` 帶 `registry=registry`；`work_actions._retry_build_action` 刪 inline `any(...)`，改呼叫同一實作並帶 `registry=workflow_registry`；對唯一 mapped OpenSpec change，在 reset／派 builder 前用 `run.workspace_root` 上 exact `run.candidate_head` Git tree 檢查 active change 與 matching archive entry 並存，命中仍派工且在 action response `warnings` 明列狀態；不能 inspect／解析時明確 raise、不得靜默當成無並存。mapped change 數不是 1 時檢查不適用、既有 retry-build 行為不變。post-archive 分支插入 design D3 指定的 `re-created active change directory` 句，其餘文字逐字保留；`_planning_artifact_relative_path_after_archive` 不動。
- [x] **T4 source／archive 結果 fail-closed（R4、R5、R6、D4、D5）**：ship 守衛 archive 後 stdout／stderr 含 `Aborted` → `RuntimeError("official OpenSpec archive aborted: ...")`。`_commit_archive_and_require_reverification` 在 `git add` 前驗 active 目錄已消失且 `changed` 含 `openspec/changes/archive/<entry>/...`（`<entry> == change` 或以 `-<change>` 結尾），否則 `RuntimeError`（含 `official OpenSpec archive relocation missing`），不 commit／harvest／建 job／reset；ship 守衛在 archive-applied 且 active 與對應 archive entry 並存時 `RuntimeError`（含 `re-created active OpenSpec change`），不跑 archive、不進 `_ship_action`；active 存在但無 archive entry 的重入路徑不變。
- [ ] **T5 tests／回歸**：T1 新測試覆蓋 retry-build warning 命中、exact candidate tree snapshot／未提交工作目錄負例、tree inspection failure 時未派工；`tests/test_work_bridge.py`、`tests/test_preflight_closeout_order.py`、`tests/test_ship_out_of_builder_clone_653.py`、`tests/test_ship_phase_harvest_649.py`、`tests/test_workflow_production_wiring.py`、`tests/test_work_actions.py`、`tests/test_ship_lane_no_openspec_911.py`、`tests/test_reviewer_candidate_tree_650.py` 全綠、不改既有斷言。全套 `pytest -q` 綠；T1 新檔全綠；確認 feature-oneshot（宣告 step）路徑的 predicate parity 參數化測試答案未變。
- [x] **T6 documentation／changelog／CLI help**：新增 `changelog.d/fix-standard-archive-step-evidence.md` 並同步 `CHANGELOG.md [Unreleased]`（bullet 含 `fix-standard-archive-step-evidence` 與 #885）。本票不新增 CLI，`cortex work --help` 輸出不變並以 help smoke（`tests/test_work_cli.py` 的 `cli.main(["work", "--help"]) == 0` 或實跑）驗證；`docs/unified-work-lifecycle.md` 的 retry-build 段補「派 builder 前按 exact Candidate Git tree 偵測 active/archive 並存，命中時回傳 operator warning、仍派 repair；archive-applied 依宣告 step 或 Manager archive job 證據判定」，ship audit 段補「未宣告 `openspec-archive` step（如 fix-standard）時，archive-applied 以恰好一筆 Manager archive job 證據＋Git ancestry 判定」，local-closeout 段補「`openspec archive` Aborted 或未搬移 active change 一律 fail-closed、不產生 archive commit；ship 段對 archive 後 active/archive 並存保留 fail-closed backstop」。
