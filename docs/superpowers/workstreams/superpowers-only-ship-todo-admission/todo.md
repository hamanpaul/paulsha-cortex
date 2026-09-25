---
status: draft
work_item: superpowers-only-ship-todo-admission
domain_breadth: 2
state_consistency: 2
invariant_count: 12
artifact_classes:
  - source
  - tests
  - documentation
---

# Superpowers-only ship Todo admission 與既有 run 恢復 Todo 草案（#1051）

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1051`；[spec](../../specs/superpowers-only-ship-todo-admission-spec.md)、[design](../../specs/superpowers-only-ship-todo-admission-design.md)。
- 此包目前是 draft、預估 10／Red。不得標記 accepted、intake、dispatch 或視為實作授權；須先依下方拆票提案建立 issue-backed child，再分別 review／重算 sizing。
- Git 基準：此分支以 latest `origin/main` `6a32a3e5e0af841794f340313c11f60f2999f6ae` 為基礎；live #983 run `workflow-52d048b72adbd5cae06f` 已有 exact Candidate `7ba7e877c94ff4eee72ba796ea9f8962953ed5cc` 和 PR #1049，WorkAuthority 為 PR=1、OpenSpec=0、Todo=0。#983 綁定的是 issue 與 Superpowers spec/design/plan，沒有獨立 canonical workstream Todo；#1051 是缺少 Todo source，不是 PR 數量問題。
- 主要交界：`paulsha_cortex/coordinator/work_actions.py` admission／ship diagnostic；`work_bridge.py`／`manager.py` planning-to-build transition；`claim.py`／Monitor source revisions 與 claim/job evidence；GitHub PR facts。完整範圍橫跨多個 owner，因此 Red。
- 不改 #983 delivery journal 契約，不操作 #983 正式 run／PR #1049，不處理 #1049 main conflict（#972／#973），不把 #911 的無 OpenSpec ship 支援回退，也不把 #810 的 post-merge checkbox closure 問題併入。
- 不新增 CLI；README／lifecycle docs、changelog fragment、policy context 和 CLI help 不變確認須納入交付 task。
- `.cortex/work-items.yaml` 的本列只提供 source binding；override 寫入本身不代表 Monitor snapshot 已更新或 work item 可 intake。

## Tasks

- [ ] **T1 tests／build admission RED（R1、R5）**：建立 isolated fixture，令 planning artifact accepted 且 `mapped_todo_paths=0`；從真實 Manager plan→build transition 斷言回報 `missing-todo-source`、builder job/worktree/agent dispatch 數量皆為 0。另測多 Todo 為 `ambiguous-todo-source`，唯一 Todo 才進 builder。
- [ ] **T2 source／Todo authority 規則（R2、D2）**：固定 WorkAuthority 的唯一 mapped Todo path 為交付 source；Superpowers spec/design/plan、basename `todo.md`、checkbox 或 `planning_authority` 不可單獨形成 mapping。有效 path 必須先是 owner 發布且具 issue provenance、`work_item` metadata 與具體交付 tasks 的 canonical `docs/superpowers/workstreams/<slug>/todo.md`；`--kind path` 只連結既存 scanner source，不建立 Todo。還須通過 canonical source parser、symlink/path guard 和 fresh Monitor snapshot 確認；不能用未 refresh 的 override 放行。
- [ ] **T3 code／Manager pre-build gate（R1、D3）**：在第一張 builder card 派工及 workspace/job 建立前讀同一 WorkAuthority，要求恰一個 mapped Todo path；0／>1 分別回穩定 reason，沒有部分 dispatch 或 agent side effect。保留 `_ship_action` 的既有精確數量 backstop。
- [ ] **T4 diagnostics／可執行修復路徑（R3）**：在 build admission 與既有 ship-stop 上產生 zero／multiple 專屬提示。zero 對尚未開工的新 work，提示 owner 先發布具 issue provenance、matching `work_item` metadata 與具體 tasks 的 canonical `docs/superpowers/workstreams/<slug>/todo.md`，再用 `cortex work link <work_id> --repo <owner/repo> --kind path --ref <repo-relative-todo-path>` 把既存 path 加入 override；此 CLI mutation 不會建立 Todo 或立即更新 WorkAuthority，必須等 Monitor correlation 產生 fresh snapshot，再由正式 start/intake admission 判定。對已有 run/candidate/PR 的工作，不提示直接 resume/re-intake，交由 recovery Child B 的明確流程。zero 不得建議 unlink；multiple 才可建議 unlink 多餘 mapping。內容需含 reason、缺少／歧義數量、authority ref 與合法 next action。
- [ ] **T5 recovery／#983 exact Candidate protocol（R4；拆為獨立 child）**：先在 recovery child 凍結同 run CAS 或新 run adoption 的唯一合法流程；比對 run id、claim key、old/new `source_revisions`／authority digest、exact candidate／verified head、job bindings、PR number/head 與 delivery binding。逐類宣告舊 verify／review／delivery evidence 保留或失效，必要時從同一 exact candidate 重驗。任何 conflict／unknown 不得改寫 registry、journal 或 source revision。
- [ ] **T6 tests／完整邊界矩陣（R5）**：沿真 Manager／WorkAuthority fixtures、stub GitHub facts，不呼叫 live service，覆蓋 (a) Superpowers-only；(b) 無 OpenSpec；(c) 唯一 Todo；(d) 缺 Todo；(e) 偽造／未確認 path link；(f) 已有 PR 的停止恢復。斷言 mapping CAS、claim/job/evidence 失效範圍及拒絕重複 push／建立 PR／merge；所有 ambiguous/unknown case fail-closed。
- [ ] **T7 documentation／changelog／CLI help／policy and test gates**：同步 `README.md`／`docs/unified-work-lifecycle.md` 的 Superpowers planning source 與 delivery Todo 區別、缺少 Todo 的正式修復方式及 recovery 限制；確認 CLI help 是否改變（本包預設不新增 CLI）；新增 branch slug 對應 `changelog.d/ship-todo-admission-plan.md`，同步 `CHANGELOG.md [Unreleased]`；跑 PR-context policy check、plan completeness/review/sizing、OpenSpec specs validation，以及每個 child 自有的 tests。

## Proposed issue-backed split

1. **Admission child**：建議票名 `fix(work): Superpowers-only work 在 builder dispatch 前要求唯一 Todo source`；owner 為 Manager admission/diagnostic；WorkAuthority／Monitor snapshot 是唯讀 source of truth。只接 R1–R3、T1–T4 及 AC1–2、AC4 前五種 source/build admission case（Superpowers-only、無 OpenSpec、唯一 Todo、缺 Todo、偽造或尚未反映的 path link）。
2. **Recovery child**：建議票名 `fix(recovery): Todo authority 加入後安全恢復已有 candidate 與 PR 的 run`；owner 為 Manager recovery，需與 claim/evidence/delivery owners 協作並以 GitHub PR facts 作遠端事實。硬依賴 Child A 已落地的 admission/diagnostic contract；只接 R4、T5–T6 及 AC3、AC4 的既有 candidate/PR case，凍結 WorkAuthority/claim CAS、舊證據 invalidation/reverification 和 no-duplicate external effects。

兩張 child issue 尚未建立；待維護者核准此 Red split 後，分別建立並將各自的 issue number、範圍和 dependency 寫回新的 accepted planning packet。此 draft 不以空泛「先研究」取代原 issue AC；child B 必須完成明確可執行恢復流程，否則 #1051 不可宣稱完成。

## Five-dimension sizing — projected Red

| Dimension | Score | Basis |
|---|---:|---|
| `domain_breadth` | 2 | Manager、WorkAuthority/claim、delivery/recovery 多模組。 |
| `state_consistency` | 2 | 來源 revision、claim/job、evidence、delivery journal 與外部 PR 的 CAS/identity。 |
| `acceptance_surfaces` | 2 | `fix-standard` gate spine 與 R-09／R-16／R-19 的固定 process-rule 輸入。 |
| `spec_stability` | 2 | packet 維持 draft；in-flight recovery 的唯一 CAS／invalidations 路徑尚待獨立 child 定案。 |
| `orchestration` | 2 | 多 cards／persona、Manager 與 Monitor authority 交界。 |
| **Total** | **10 / Red** | `stability-risk-v2`；完整 parent scope，不是拆分後 child 的最終 score。 |

## Required gates before any child is accepted

- [ ] Spec、design、Todo 都有 `status: accepted`，且同一 `work_item`；當前 draft 不能因格式完整被當成 accepted。
- [ ] `assess_planning_completeness()` 全部 accepted 且無 blocking marker。
- [ ] `plan_review_gate()` completeness、R-09/R-16/R-19/R-22 compatibility、builder envelope 都通過；若 envelope unavailable，記錄實際 bypass evidence。
- [ ] 以當時 mapping、combo、gate spine、contract rules、cards/persona binding 實跑 `compute_sizing_score()`；Red 保持拆票，禁止改低宣告值以換 Green/Yellow。
- [ ] 各 child 有 issue-backed Boundary、完整 acceptance matrix、code/tests/documentation tasks 和獨立 review；不得把 #911／#810／#983／#765 的邊界混成一票。
- [ ] merge 前完成該 PR 的 commit changelog fragment、CHANGELOG entry 和 PR-context policy gate；此 draft PR 僅記錄 planning proposal。
