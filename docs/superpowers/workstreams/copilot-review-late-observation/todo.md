---
status: accepted
work_item: copilot-review-late-observation
domain_breadth: 0
state_consistency: 0
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# Copilot 準時提交、晚觀測的 exact-HEAD review（#1020）

## Boundary

- Issue：[hamanpaul/paulsha-cortex#1020](https://github.com/hamanpaul/paulsha-cortex/issues/1020)。唯一 Work ID：`copilot-review-late-observation`；draft mapping 見 `work-item-mapping.yaml`。
- 預期 production 只有 `paulsha_cortex/coordinator/delivery.py` 的 `ReviewLoop.record_review()`。`work_actions.py` 是現有 caller 與 integration-test surface；除非 RED 證明 caller 還需改動，不能順手改 source。
- 不變更 #948 adoption、#871 maintainer fallback、15 分鐘時限、review/thread 定義、remote delivery/final gates、durable state/schema、CLI 或 PR #986 狀態。
- Accepted spec/design/本 Todo 是 planned authority；實作中只翻 checkbox。若發現必要新範圍或未決決策，停在該界線、記錄證據並新開 issue，不以改變原 issue 解讀來擴權。

## 現場證據

- live #1020：request `17:42:59 UTC`；同 exact HEAD review `17:45:41 UTC`（+162s）；Manager 於 `17:58:43 UTC` 觀測（+944s）；安裝版 Cortex 0.1.10 的 `ReviewLoop.record_review()` 重現 false timeout。
- Base `e74750dd74065fe0c911069adf609a4238148d47`：`delivery.py:224-227` 以 observation-request elapsed 判 timeout；`work_actions.py:6097-6154` 命中 current review 後確實把兩個 epoch 傳入；`github_delivery.py:128-172` final gates 獨立阻擋 mergeability、checks、review/thread 等。
- `tests/test_delivery_orchestrator.py:264-277` 已有晚提交逾時回歸；`tests/test_github_delivery.py:167-184` 已有 fail-closed final gate matrix；`tests/test_copilot_review_adopt_existing.py` 鎖定 #948 adoption 相容性。

## Five-dimension sizing

| Dimension | Value | Basis |
| --- | ---: | --- |
| domain_breadth | 0 | 一個 production module、同一 delivery 判定責任域。若 integration 要求改另一 production module，重算。 |
| state_consistency | 0 | 只讀 request/review timestamps 並計算；不新增或更新 durable fields、journal、schema。 |
| acceptance_surfaces | 2 | Repo `fix-standard`: gate_spine=2，加 R-09/R-16/R-19，共 5 個 signal。 |
| spec_stability | 0 | `assess_planning_completeness()` 實測三種 artifact 全 accepted、無缺項/阻塞標記。 |
| orchestration | 2 | Repo `fix-standard`: 9 張 cards，9 張有 persona_binding。 |

Repo sizing helper 結果：`current_sizing_snapshot(workspace_root=/tmp/cortex-1020-plan, combo_name=fix-standard, artifact_rows=[spec, design, plan])` → **(4, yellow)**；直接 `compute_sizing_score()` breakdown 為 **0 + 0 + 2 + 0 + 2 = 4 / Yellow**。這是目前完整 triad 的 planning projection，不是實作後的 run sizing；派工前按當時 combo/applicability 重算。若 helper 回傳 unavailable，先找出 artifact mapping/格式缺口，不能填 0。若分數到 Red，完整保留 issue acceptance 並提 issue-backed split。

## Tasks

- [ ] **T1 RED / ReviewLoop regression**：新增或擴充 focused 測試：request=1000、exact-head review submission=1162、observation=1944 必須 `passed`；submission=1900（deadline exact）在 late observation 時必須 `passed`；submission=1901 必須 `needs_human / copilot-review-timeout`。先確認原實作在 #1020 案例必 RED。
- [ ] **T2 source / request-bound deadline**：在 `delivery.py::ReviewLoop.record_review()` 僅對 `adopted_at is None` 以 `submitted_at - requested_at` 判 900 秒 deadline；維持 `< requested_at`、`> now`、exact HEAD、finite epoch 與 review ID 驗證順序/語意。已採信 (`adopted_at != None`) 維持原 `now - adopted_at` 判式。
- [ ] **T3 RED/GREEN / no-review and identity negatives**：期限後沒有 current review 仍 timeout；早於 request 或 future submission 不通過；old HEAD、error review 不能通過。既有 #948 adoption test 保持原斷言。
- [ ] **T4 integration / 真實 `_ship_action` caller**：沿 `tests/test_copilot_review_adopt_existing.py` fake GitHub fixture 加獨立 #1020 case：ship journal 保存 request epoch，remote 回同 HEAD/Copilot/current review（+162s），Manager now 為 +944s；確認不落 `copilot-review-timeout` 且只沿既有 review 流程前進。不得呼叫真 GitHub、真模型或合併真 PR。
- [ ] **T5 guardrails / final gate**：current unresolved non-outdated blocking thread、PR dirty/head mismatch、missing/non-terminal-green checks 仍阻擋 merge authorization；沿用並保留 `tests/test_github_delivery.py::test_delivery_gate_fails_closed` 等既有 gate tests。`merge_if_ready` 不可因 ReviewLoop 新判式而在這些負例呼叫。
- [ ] **T6 docs / policy**：新增 `changelog.d/copilot-review-late-observation.md` 並同步 `CHANGELOG.md [Unreleased]`；在 `docs/unified-work-lifecycle.md` 說明 review deadline 依 submitted_at、Manager 輪詢延遲不重定時，無回覆與 late submission 仍 timeout。CLI 不新增命令或參數；policy 要求的 CLI help、R-09/R-16/R-19 證據依 PR context 收集。
- [ ] **T7 delivery evidence**：focused tests 與 full suite、repo preflight、PR-context policy check、exact-head CI、獨立 code review、mergeability 和所有既有 ship gates 都逐項記錄。PR body `Closes #1020`；不得用單一綠燈代替其他 gates。未合併不得標完成。

## Sizing re-evaluation

本 draft 的 frontmatter 是 `domain_breadth=0`／`state_consistency=0`；本次 repo helper 實測總分 4 / Yellow。只有 production module 數、durable state 範圍、accepted artifact 完整度或 combo/applicability 變動時才重算；任何 scope 擴大均需 issue-backed 追蹤。
