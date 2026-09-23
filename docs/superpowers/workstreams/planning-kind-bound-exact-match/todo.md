---
status: accepted
work_item: planning-kind-bound-exact-match
domain_breadth: 0
state_consistency: 1
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# Planning 產出目的地精確綁定：`planning_kind_bound` 去除 substring glob（#812）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#812`；[spec](../../specs/planning-kind-bound-exact-match-spec.md)、[design](../../specs/planning-kind-bound-exact-match-design.md)。修 #802（`planning-artifact-manifest-binding`）落地的 substring glob 目的地綁定。
- 觸及模組（1 個 production 模組 → `domain_breadth: 0`）：`paulsha_cortex/coordinator/manager.py` 的 `planning_kind_bound`、新增 `_planning_anchor_slugs`、`_publish_planning_artifacts`、`_validated_brainstorm_planning_authority`，以及 `apply_workflow_action` 內 brainstorm `artifact_writer` 的呼叫端。`state_consistency: 1`：判準決定哪些 ref 進入持久化的 `run.planning_authority`，已持久化的 ref 不重驗。
- 本票明確授權修改的非本 work item 文件：`docs/superpowers/specs/planning-artifact-manifest-binding-spec.md`（第一條 MUST）、`docs/superpowers/specs/planning-artifact-manifest-binding-design.md`（實作點）、`CHANGELOG.md [Unreleased]` 與 `changelog.d/planning-artifact-manifest-binding.md` 的 #802 條目（只改 spec R6 指定的兩句）。`docs/superpowers/workstreams/planning-artifact-manifest-binding/todo.md`、`docs/superpowers/plans/planning-artifact-manifest-binding.md`、`openspec/changes/archive/2026-08-27-planning-artifact-manifest-binding/**` 不動。
- 不改：`planning_runtime._planning_destinations`；deck 卡片／combo 的 `*<task-slug>*` 樣板與 build 端 declared input glob（#937 6.4）；`openspec_bound` 的 `parts[2] == work_id` 規則（#937）；`_materialize_plan_card_output`；#847 source ownership guard；`DiagnosticReason` schema、`from_dict`、`__post_init__`；planning failure 分類。不新增 CLI、不改 persisted schema。
- spec／design／本 todo 文字是 pinned authority，只准把 `[ ]` 翻成 `[x]`；有需要澄清的地方寫進 terminal reason。
- 留在 Manager 已 checkout 的分支上工作，不得另建 `wt/...` 分支。
- 不得 commit 或刪除 `docs/superpowers/plans/planning-kind-bound-exact-match.md`（Manager materialize 的 plan 副本）。
- 測試與文件不得硬編 `openspec/changes/<change>/` 路徑；需要 openspec 路徑時，從 `compile_combo` 產出的 manifest 或 `run.openspec_refs` 推導。

## 現場證據

- #812：main `0f57165c` 直呼 `planning_kind_bound("spec", "docs/superpowers/specs/feat-work-gc-v2-spec.md", "feat-work-gc")` 回 `True`；`feat-work-gc` 與 `feat-work-gc-v2` 都是 `.cortex/work-items.yaml` 裡真實存在的 work item。
- 現行 main `7fa4716b`：`manager.py:8933-8973` 以 `f"…/*{work_id}*-{kind}.md"` 搭配 `fnmatch` 比對；`manager.py:9018-9027` 用 `not (manifest_bound or kind_bound)`，combo manifest 的 `*<task-slug>*` 樣板可以單獨放行；`manager.py:3436-3444` 的 authority 重驗也用 `declared_patterns` 或 `planning_kind_bound`。
- offline 重現：`_publish_planning_artifacts(work_id="feat-work-gc")` 寫 `docs/superpowers/plans/feat-work-gc-v2.md`，在 fix-standard、small-fix、feature-oneshot 三種 manifest 下都被接受；small-fix、feature-oneshot 下 kind=spec 寫 `feat-work-gc-v2-design.md` 也被接受（`planning_kind_bound` 為 `False`）。只修 `planning_kind_bound` 關不掉這個洞。
- `.cortex/work-items.yaml` 128 個 work item 中有 25 個的 openspec change ≠ work_id（例：`workflow-execution-identity-producer`→`cortex-refine-complete`），合法目的地必須靠 run 的 anchor slug 接受。
- prototype（scratch 副本）照 spec 實作後跑全套：只有 `test_planning_kind_bound_accepts_glob_bound_destinations` 的 3 組 parametrization 失敗（屬預期，需改斷言）；其餘 12 個失敗是 scratch 副本不是 git checkout 造成，baseline 副本同樣失敗。

## Tasks

- [ ] **T1 tests／RED**：新增 `tests/test_planning_kind_bound_exact_match.py`，斷言逐條對應 spec R7 (a)–(g)；
      manifest 用 `compile_combo`（沿 `tests/test_planning_artifact_manifest_binding_802.py` 的 `_manifest_outputs`）、authority 重驗用同檔 `test_fix_standard_authority_accepts_published_canonical_planning_triplet` 的 `SimpleNamespace` run＋brainstorm evidence 樣板。
      同時把 802 檔 `test_planning_kind_bound_accepts_glob_bound_destinations` 的 prefix／suffix／middle 三個 `is True` 改成 `is False`（canonical／dated 保留）。
      在現行 main 必須 RED：(a)(b)(c)(e) 被接受（(c) 的未知 kind 在 main 是 `unknown planning artifact kind`，訊息斷言 RED）、`_planning_anchor_slugs` 與 `anchor_slugs` 參數不存在；(d) 的 `work_id="demo-plan"` 案例在 main 已是預期值，屬回歸防護，允許一開始就 GREEN。
- [ ] **T2 source／精確 stem 文法（R1、R5、D1、D5）**：改寫 `planning_kind_bound(kind, path_value, work_id, *, anchor_slugs=())`：
      守衛與順序保留；spec／design 只收 `<base>-<kind>.md`，plan 只收 `<base>.md`／`<base>-plan.md`；`<base>` ∈ `{work_id}` ∪ 合法 anchor ∪ `YYYY-MM-DD-<work_id>`（`[0-9]`，不用 `\d`）；
      plan 以候選集合判定（`stem` 與「剝掉一次 `-plan` 的 stem」並存，任一命中即可），不得無條件剝除，`work_id="demo-plan"` 的 `plans/demo-plan.md` 必須維持 `True`；
      slug 正則抽成模組常數，`work_id` 與 anchor 共用；移除 `fnmatch`；函式 docstring／註解改寫成如實描述，註明 `is_absolute`／`..` 是被家族守衛涵蓋的 defense-in-depth。
- [ ] **T3 source／anchor 與 publication（R2、R3、D2、D3）**：新增 `_planning_anchor_slugs(run)`（`run.openspec_refs` ＋ `run.planning_authority` 中本 work item 恰五段的 `docs/superpowers/workstreams/<slug>/todo.md`，過濾 slug 正則，排序去重，以 `getattr` 相容 `SimpleNamespace`）；
      `_publish_planning_artifacts` 新增 keyword `anchor_slugs: tuple[str, ...] = ()`，放行條件改成 design D3 的形狀（docs 家族只認 `kind_bound`，openspec 維持 `manifest_bound`），拒收訊息不變（唯一例外見 spec R3：docs 路徑未知 kind 改在路徑判定以 `outside governed roots` 被拒）；
      brainstorm `artifact_writer` 傳入 `anchor_slugs=_planning_anchor_slugs(run)`。
- [ ] **T4 source／authority 重驗（R4、D4）**：`_validated_brainstorm_planning_authority` 對 `existing is None` 的新 ref，docs 家族只用 `planning_kind_bound(kind, ref, run.work_id, anchor_slugs=_planning_anchor_slugs(run))`，其他 ref 維持 `declared_patterns`；
      `existing` 分支、#418 materialized plan 副本的 `missing` 判定、拒收訊息格式都不變。
- [ ] **T5 tests／回歸**：`tests/test_planning_artifact_manifest_binding_802.py`（只有 T1 那三個斷言翻轉）、`tests/test_workflow_production_wiring.py`、`tests/test_planning_publication_transaction_536.py`、`tests/test_claim_inflight_supersede_524.py`、`tests/test_diagnostic_invariant_family_527.py` 全綠，不改其他斷言；
      再跑 repository 全套測試確認沒有其他回歸。
- [ ] **T6 documentation／#802 文件與 changelog 更正（R6、D6）**：`docs/superpowers/specs/planning-artifact-manifest-binding-spec.md` 第一條 MUST 與 `-design.md` 實作點改寫成 R1 文法，並明寫不得以 substring glob 比對（#812）；
      `CHANGELOG.md [Unreleased]` 與 `changelog.d/planning-artifact-manifest-binding.md` 的 #802 條目只更正兩句：DiagnosticReason v2 降級警語（任何 `schema_version: 2` 記錄都會被舊版拒收）、kind-bound 那句改指 #812 精確 stem；
      `docs/unified-work-lifecycle.md` 在「Planning capability probe boundary」段後補一小節「Planning 產出目的地綁定（#812）」，說明精確 stem 文法、anchor 來源，以及 combo manifest 的 `*<task-slug>*` 不再單獨放行 `docs/superpowers/{specs,plans}`。
- [ ] **T7 documentation／changelog／CLI help**：新增 `changelog.d/planning-kind-bound-exact-match.md`，並同步 `CHANGELOG.md [Unreleased]` 的 #812 條目（精確 stem、三個入口一起關、change slug ≠ work_id 仍接受、#802 條目已更正）；
      本票不新增 CLI，`cortex work --help` 輸出不變；以 help smoke 驗證（實跑 `cortex work --help` 並跑 `tests/test_cli_help_alignment.py`，兩者皆不需修改）。
