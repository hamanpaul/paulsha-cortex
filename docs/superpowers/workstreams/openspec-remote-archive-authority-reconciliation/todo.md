---
status: accepted
work_item: openspec-remote-archive-authority-reconciliation
domain_breadth: 0
state_consistency: 0
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# OpenSpec remote-archive authority reconciliation（#961）

## Boundary

- Issue：[hamanpaul/paulsha-cortex#961](https://github.com/hamanpaul/paulsha-cortex/issues/961)。Parent：[ #887](https://github.com/hamanpaul/paulsha-cortex/issues/887)，完整 R1–R8 維持 aggregate acceptance；本票負責 R5、R7、R8(e)–(f)。
- Dependency：#961 是先行 Child A；#962 是 Child B 並已標為 blocked by #961。#962 只有在本票合併後才可整合 completion recovery；#887 不因 #961 單獨完成而關閉。
- Frozen authority：本目錄的 accepted spec/design/todo 對應 live #961；實作時只翻 todo checkbox，其餘變更走正式新 review，不要在 red-to-green 途中暗改 acceptance。
- Production：僅 paulsha_cortex/coordinator/claim.py 的 semantic-source aggregation/arbitration 和 module logger。無 registry/journal/outcome writes、無新 persisted 欄位/schema。
- Tests：新增 tests/test_post_merge_authority_restart_guard.py 的 authority arbitration cases；既有 tests/test_work_claim.py、tests/test_claim_provider_scope_530.py、tests/test_monitor_work_review_regressions.py 作回歸。#962 後續會依賴本票，在同一新測試檔增加 run recovery cases。
- Docs/changelog：後續實作新增 changelog.d/openspec-remote-archive-authority-reconciliation.md、更新 CHANGELOG.md [Unreleased] 與 docs/unified-work-lifecycle.md；記錄僅在 exact remote merged-with-merge-commit proof 成立時採 archived。
- 不得在測試或文件寫死 OpenSpec change 的 workspace 路徑；fixtures 以參數化 ref/change name 建構 source。
- 本 todo 不涵蓋 #962 sizing、parent closeout、runtime deployment 或 service restart。

## 現況證據（2026-09-23）

- 程式基準為 main commit 3e23f9dc4fcd2c0b19d9a46ffc13d4540e681d4d；下列 source 與測試觀察以該版本的受版本控制檔案為準。
- live #961 與 #962 均 open；#962 正確標記 blocked by #961；parent #887 更新後仍 open 並要求兩 children landed 加完整整合驗收才完成。
- claim.py:160-169 將本地與 GitHub OpenSpec sources 折成同一 semantic key；:735-762 目前採第一個 revision 並在另一個值出現時即以 row-malformed/source_revisions 拒絕。
- providers.py:221-229 的 RepoWorkProvider 產生 repo:{repo}／active source；:1715-1731 的 GitHub terminal tree 產生 github-terminal:{repo} OpenSpec sources；:1733-1792 與 :1808-1812 建立並驗證帶 merge ancestry 的 remote_prs rows。
- 全 tests 精確搜尋沒有「confirmed semantic work authority revisions conflict」專項 regression。tests/test_monitor_work_review_regressions.py 已有可參照的 GitHub OpenSpec 與 remote_prs fixture shape；不直接依賴網路。
- 五維計分依目前 planning.compute_sizing_score、work_bridge sizing wiring、deck task-types.yaml/cards.yaml 核對。issue #961 是 fix 類，task type fix 選 fix-standard；該 combo 的 9 張 card 均有 persona_binding，gate_spine 為 2；R-09/R-16/R-19 三條 process rules 皆進 acceptance signal。

## 五維 sizing（本 child scope）

| 維度 | 值 | 依據 |
|---|---:|---|
| domain_breadth | 0 | 一個 production module claim.py，單一 coordinator domain |
| state_consistency | 0 | 純 snapshot arbitration；不新增／寫入 durable state |
| acceptance_surfaces | 2 | fix-standard gate_spine=2，加 applicable rules 3，signal=5，門檻映為 2 |
| spec_stability | 0 | 三份 accepted artifacts 完整且沒有 blocking marker |
| orchestration | 2 | fix-standard cards=9、persona bindings=9 |
| **total / band** | **4 / Yellow** | score=4；sizing_band(4)=Yellow |

Sizing inputs were read from current source, not copied from the #887 aggregate. invariant_count=7 counts the child’s seven testable invariants below; artifact_classes lists touched deliverable types and are not additional dimensions. Parent #887 stays 7/Red at aggregate scope. #961 needs no further split at 4/Yellow; #962 still needs its own five-dimensional calculation before dispatch and is not assessed here. If the runtime-selected combo changes from fix-standard, re-run the same score function with the selected manifest inputs rather than retaining this projection.

## Invariants／驗收

1. Only key openspec:{repo}:{ref} with exactly active and archived identity values is eligible for arbitration.
2. Archived contributors all come from github-terminal:{repo}; active contributors all come from repo:{repo}.
3. Confirmed github_pr sources are non-empty and each has status closed or merged.
4. An ok github-terminal snapshot with valid observations.remote_prs list contains exactly one matched source_id row with merged_with_merge_commit is True for every confirmed PR source; duplicates fail closed.
5. Successful authority output is equal to removing the local active source: archived source revision, digest, and mapped_openspec match; equal source membership elsewhere is unchanged.
6. Input source order does not affect output; each resolved key emits exactly one sanitized warning with no path.
7. Every ineligible/malformed/other-key conflict preserves the original AuthorityValidationError message, row-malformed reason_code and source_revisions field.

## Tasks

- [ ] **T0 acceptance freeze and fixture inventory**：核對 live #961/#887 dependency與 current source evidence；準備 canonical snapshot fixtures，明確標示 repo provider、terminal provider、confirmed github_pr 及同 source_id remote_pr row。不得新增或 commit 本票 scope 外的檔案。
- [ ] **T1 tests／RED**：在 tests/test_post_merge_authority_restart_guard.py 加 Child A public-boundary tests。正例要證明嚴格 proof 下選 archived，輸出等於移除 local source；負例逐條否定條件並精確斷言錯誤欄位。先確認現行 setdefault 路徑對 active+archived fixture RED。
- [ ] **T2 source／兩階段收集與裁決**：在 claim._authority_from_canonical_row 先 collect observed values per semantic key，再呼叫純 _merged_remote_archive_resolution。一般單值沿用原值；只有 spec R961.2 全部條件成立回 archived；其他衝突 raise 現有 AuthorityValidationError。semantic_source_revision 的過濾/驗證與 mapped_openspec aggregation 不改。
- [ ] **T3 audit／determinism tests**：測來源列正反順序、source_revisions/digest/mapped_openspec exact equality、每 key 一行 warning 及 diagnostic label 路徑清理；確保無 provider network、registry write、filesystem mutation。
- [ ] **T4 R7 fail-closed matrix**：覆蓋 no PRs、PR status/remote merge 不正面、remote_prs 缺席／錯型／source_id 不匹配／同 source_id 重複（皆 true 與 true/false）、terminal provider missing/degraded、provider role swapped、third value、non-OpenSpec conflict；每例維持同 message/reason_code/field。
- [ ] **T5 regression**：保留並執行 tests/test_work_claim.py、tests/test_claim_provider_scope_530.py、tests/test_monitor_work_review_regressions.py 的既有斷言；新增測試只改新 fixture，不以放寬舊 oracle 轉綠。
- [ ] **T6 docs/changelog/policy**：依 repo policy 加 changelog fragment 並同步 CHANGELOG.md [Unreleased]；docs/unified-work-lifecycle.md 說明唯一允許收斂的 source/provider/PR-ancestry 條件與其他衝突 fail-closed。CLI 沒有變更，驗證 cortex work --help 輸出未變。
- [ ] **T7 PR gates and child handoff**：在實作 worktree 執行 focused tests、上列 regression tests、全 repo required CI 與帶 PR context 的 policy_check；PR body 使用 Closes #961 並完成 repo checklist。合併後通知/解鎖 #962 dependency；不關閉 #887，完整 parent AC 留待兩 child integrated gate。

## Completion boundary

#961 可在其所有 acceptance tests、documentation/changelog、policy/CI 與 review 通過後完成。成功只代表 R5/R7/R8(e)–(f) 已落地；不得宣稱新舊 merged run completion、#887 aggregate outcome 或 loaded runtime completion 已完成。#962 與 #887 的其餘驗收仍 open until their own evidence passes.
