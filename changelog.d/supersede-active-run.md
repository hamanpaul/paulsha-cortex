---
type: fix
scope: coordinator
---
**Issue #524：planning 成功的 in-flight run 被自行 supersede，其產出又使後續世代 fail-closed**

生產現場（work_id `fix-brainstorm-revalidation-diagnostics`／issue #514，
2026-08-14 04:55–05:00）：`workflow-009fe9ab303df196209d` 04:55:07 claim，
`workflow-claim`／`brainstorming`／`openspec-propose`／`writing-plans` 四張卡全
`passed`、phase 已達 `build`，卻在 04:56:42 被系統自行標成 `superseded`——facets
只有 supersede 順手加上的 `blocked`，沒有任何 work-abandon evidence。同一毫秒建立
的 `workflow-952a3652afc51ab4f29c` 與其後的 `workflow-7bb3a83c2c1fc37359d5` 都只
完成 `workflow-claim`，四分鐘內用掉全部三次語意 re-claim 額度（`#519` 熔斷），
無一次失敗肇因於工作項本身。

## (A) 根因：run 被自己的成功產出擠掉識別

`claim_key` 與 `run.source_revision` 都由 `claim.work_authority_digest()` 導出，
而該 digest 折入 `source_revisions`。monitor 的 repo provider 以
`docs/superpowers/specs/**/*.md`／`docs/superpowers/plans/**/*.md` 掃描產生
`superpowers_spec:`／`superpowers_plan:` source——**run 自己的 `brainstorming`／
`writing-plans` 卡一旦把 spec/design/plan 寫進 governed roots，下一輪 correlation
就把它們當成新的 confirmed source 併進同一個 work item**，digest 因此改變、
`claim_key` 隨之漂移。

以現場 snapshot 反算，三個世代的 `claim_key` 逐字對應三種 source 集合：

| run | 對應的 planning-artifact source 集合 |
| --- | --- |
| `workflow-009fe9ab303df196209d` | 無（claim 當下尚未產出） |
| `workflow-952a3652afc51ab4f29c` | ＋2 個 `superpowers_spec`（spec／design） |
| `workflow-7bb3a83c2c1fc37359d5` | ＋2 個 `superpowers_spec` ＋1 個 `superpowers_plan` |

`_claim_action()` 的 active-run 偵測因此完全找不到那個 run：第一段用漂移後的
`_expected_claim_key(authority)` 比對 persisted `run.claim_key` 必然落空；第二段
fallback 雖改用不受 planning 產出影響的穩定識別，卻只在 `automatic`（auto-scan）
或 `args["action"] == "resume"` 時才跑——`start`／`intake` 這兩個 control-request
入口整段跳過。canonical_run 保持 `None`，claim 路徑把它當成全新 claim，
`registry._manager_create_workflow_run()` 再無條件把同 `(repo, work_id)` 的所有
ongoing run 標成 `superseded`。

**修法**：`_claim_action()` 新增第三段、不分呼叫端的 in-flight 保護傘，判準有二、
缺一不可——

1. **未失敗**：`run.status == "ongoing"` 且 `workflow_status(run) == "ongoing"`。
   兩個都要：`workflow_status()` 對 abandon 釋放過的 run（`superseded` ＋
   `planning_released`）刻意回傳 `"ongoing"`，只看它會把 `#256`／`#416` 的
   abandon→reclaim 出口一併鎖死；只看 `run.status` 又會誤納 needs_human／
   needs_decomposition／blocked 的 run。
2. **漂移完全來自 run 自己的產出**：新增
   `claim.authority_digest_without_planning_outputs()`，把 `superpowers_spec:`／
   `superpowers_plan:` 前綴的 source 剝掉後重算 digest，必須與 run 持久化的
   `source_revision` 逐字相符。這兩類 source 依構造永遠是 planning phase 的
   **產出**——canonical row 解析只認 `github_issue`／`github_pr`／`openspec`／
   `todo` 四種 kind，`superpowers_*` 只會被 monitor 掃出來，不可能是 operator 在
   `.cortex/work-items.yaml` 宣告的授權來源。

判準 2 同時守住既有的 operator 逃生口：issue 開關、openspec revision、todo 成員
變動等**真正的** authority 變更不會被剝除，digest 依然不同，`start` 照舊開新世代
（`tests/test_work_actions.py::test_source_change_starts_new_canonical_run` 未改動
仍綠）。以現場資料驗證：剝除後的 digest 為
`039e89aab0a56384bce29bc89dc638c4e176f96873e9a4d89627b223d79a31bf`，與被誤
supersede 的 `workflow-009fe9ab303df196209d` 持久化的 `source_revision` 逐字相符。

命中保護傘時以既有的 `#216 AC5` resume 分支收尾（該分支條件加上
`inflight_resume`）；否則會落到 `decide_manual_start()` → `_existing()`，那裡對
「persisted claim_key 與目前 authority 不符」是直接
`raise ValueError("persisted claim key does not match authority")`——等於把自行
supersede 換成一個例外，run 一樣救不回來。

`#519` 的 `SEMANTIC_RECLAIM_LIMIT` 計數邏輯一字未動。

## (B) 前代產出使後續世代 fail-closed：承接前代的 artifact authority

`#524` 原始描述推測本情境的分類仍是 `content`、連 `recover-planning` 都不受理。
實測**不成立**：`evidence/planning-recovery/workflow-7bb3a83c2c1fc37359d5-*.json`
的 `classification` 是 `environment`，`#416` 的
`_is_planning_authority_residue_failure()` carve-out 已正確涵蓋
`planning artifact lacks current planning authority`。真正的死結在更下游——
即使 `recover-planning` 受理、重跑 brainstorm，也必然再撞同一堵牆。

實際成因是 **artifact kind taxonomy 兩邊不一致**：monitor 的 provider 規則把
`docs/superpowers/specs/**/*.md` 一律標成 `superpowers_spec`，而 planning 產線的
canonical destinations（`planning_runtime.py` 的
`{"spec": …-spec.md, "design": …-design.md, "plan": …}`）把同目錄下的
`*-design.md` 定義為 kind `design`。`work_bridge._artifact_rows()` 過去把這條差異
直接抹平成 `spec`，於是新世代 claim 時 seed 進 `run.planning_authority` 的 design
檔掛著 kind `spec`；等 brainstorming 用 kind `design` 對同一路徑重新發佈，
`manager._publish_planning_artifacts()` 的 `owner.kind != row["kind"]` 立刻
fail-closed，訊息即現場的
`primary-artifact-write-rejected: ValueError: planning artifact lacks current
planning authority: …-design.md`。**下一代因此永遠承接不了前一代的 artifact
authority。**

三條候選路線中採「讓下一代能承接前代的 artifact authority」：另外兩條都治標不治本
——把 supersede 殘留納入 `#416` carve-out 只是改分類，`recover-planning` 重跑仍會
撞同一個 kind 比對；在 supersede 時回滾該代發佈則會刪掉 planning 唯一有價值的成功
產出（現場那三份 artifact 正是本 issue 的關鍵證物）。

**修法**：新增 `work_bridge._superpowers_spec_kind()`，依檔名尾綴把
`*-design.md` 還原為 kind `design`，其餘維持 `spec`。只用檔名判定（不讀內容）
——這與 `planning_runtime` 的 destinations 是同一組字面約定，也是唯一能在「尚未
讀檔」的 claim 時點取得的訊號。

**遷移註記**：現存 jobs.json 有 54 個 run 的 `planning_authority` 把 `-design.md`
記成 kind `spec`，其中 52 個已終態（`superseded`／`done`，不再被重新掃描）、1 個在
`build` phase（`start_canonical_workflow` 對非 `define` phase 早退，不會重掃）、
1 個即本 issue 的 `workflow-7bb3a83c2c1fc37359d5`（已 `needs_human`，本就需
abandon）。因此不另加相容層。

## 不在範圍

`#519` 的熔斷額度重置（另有 PR 平行處理）、`#507` 的 worktree 抹除、`#523` 的
collision。另記錄一個同類但本次未觸發的潛在缺口：run 自己的 `openspec-propose` 卡
若讓 `authority.mapped_openspec` 長出新項，`_claim_action()` 第二段 fallback 的
`run.openspec_refs == authority.mapped_openspec` 比對同樣會落空（本次現場的
work item 未宣告 openspec source，故未踩到）；新的保護傘不比對該欄位，已間接覆蓋。
