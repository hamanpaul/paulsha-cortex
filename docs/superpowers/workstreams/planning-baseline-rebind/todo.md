---
status: draft
work_item: planning-baseline-rebind
issue: 1042
domain_breadth: 2
state_consistency: 2
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# Planning baseline rebind and verify recovery Todo (#1042)

## Boundary and evidence

- Authority: live [#1042](https://github.com/hamanpaul/paulsha-cortex/issues/1042). This work item is only a draft; adversarial review must resolve the source resolver, action contract, and Red sizing before acceptance or implementation dispatch.
- Related scope: #961 is the reproduction and remains responsible for its claim-authority work. #937 owns builder-authored incremental task drift. #897 owns harvest-time planning drift and ancestor-candidate recovery. This todo does not absorb those fixes.
- Current planning base: `origin/main` at `688164d7f70688f55959abbe341753a17e4f3239`.
- PR #1039 is merged at `d399e5d531abba8cf5f649d54b6b02d0c48308b2`; it changed the #961 T6 label to `documentation` to satisfy plan-review completeness and explicitly left acceptance scope unchanged.
- Reproduction hashes: pre-PR run baseline todo `d5f9c6a288bee485fb0db3e259033da295c868d6872ac0c311076ba279d0735d`; accepted main todo `cd55c7ce9c3e202d9057f764c88a42b8fc294de4618e85e90433e5e1b3ce544e`; candidate `8defa4ca9c8f2a310b1dc37d36d69de1580ce4d4` contains todo `cb390f92be32801dd6c4d276a2d99ea96205ead5ece181995249fe582030016f`.
- Candidate spec/design bytes equal the accepted main artifacts; only todo checkbox markers differ after normalization. The exact same-run recovery is a regression target, not a claim that the now-abandoned #961 run remains recoverable.
- Latest #961 comment states old run `workflow-8a41b4bb942ad716b759` was abandoned and new run `workflow-8c7a26f12cfc6d4a196c` started from the corrected baseline and reached build. Old candidate evidence is not completion evidence for that new run.

## Owner and sizing

- Manager owns acceptance, provenance validation, and recovery eligibility in `manager._dispatch_workflow_card` / `resume_workflow_run`.
- Registry owns only the exact Manager-authorized state transition and CAS. `work_actions.py` and `control/contract.py` expose the requested recovery selectors; neither can assert accepted source provenance.
- Four expected production modules yield `domain_breadth=2`; durable baseline/evidence CAS and re-entry yield `state_consistency=2`.
- `fix-standard`: gate-spine count 2; applicable rules R-09/R-16/R-19 yield `acceptance_surfaces=2`; nine cards with persona bindings yield `orchestration=2`.
- Draft status yields `spec_stability=2`; estimated current score is 10/Red. At accepted status it remains 8/Red. The scope needs a reviewed issue-backed split decision before implementation dispatch; the full #1042 acceptance remains intact.

## Invariants

1. Only a uniquely Manager-validated canonical source snapshot reviewed by this run may replace its frozen planning authority.
2. Plan-review pass, accepted artifact hashes/provenance, baseline update, audit receipt, and build-phase transition are committed before any builder dispatch.
3. Spec/design bytes must match exactly; todo/task files may differ only under the existing checkbox-marker normalization.
4. Candidate-preserving recovery requires exact active run ID, exact candidate SHA, exact accepted source/review binding, verify phase, planning-input-drift stop, and no verify dispatch or active job.
5. Recovery preserves the candidate SHA and requires the normal verify gate; it cannot copy green test/gate evidence across runs or mark verify passed.
6. If any source, candidate, authority, or run CAS predicate is unknown or mismatched, recovery fails closed and the fresh-run path uses current confirmed authority without old evidence.
7. Recovery evidence is immutable and run/candidate/source bound; exact re-entry creates no second state transition or verify job.

## Tasks

- [ ] **T0 source/API audit**：重讀 live #1042/#937/#897/#961 與 #1039 merge facts；追出 Manager 可用的 canonical source revision、plan-review identity、WorkAuthority binding 和 Registry CAS API。若無可信 source resolver，先修規劃並保留 fresh-run-only fallback，不以工作樹 bytes 代替 provenance。
- [ ] **T1 tests / RED：plan acceptance boundary**：測 final Yellow plan-review pass 對 exact corrected spec/design/todo 寫入 accepted hashes/provenance，再推進 build；在 Registry commit 失敗時不派 builder。負例含 unmerged/unknown source、wrong work_id/kind/ref、duplicate ref、symlink、source/hash drift。
- [ ] **T2 Manager baseline capture**：在 `manager._dispatch_workflow_card` 對成功的最後 plan-review transition 建立 review-bound baseline receipt，並在同一 Registry transition 提交 `planning_authority`、`plan_review_passed`、phase/step 變更及 receipt ref。先完成 source provenance proof，再更新；不改 `_workflow_input_snapshot` 的通用漂移規則。
- [ ] **T3 tests / RED：same-run exact recovery**：以已 harvest candidate、verify 未 dispatch 的 run 測 exact run/candidate CAS、source+review revalidation、todo checkbox tolerance、候選 SHA 不變、只建立一張 normal verify job。負例逐一涵蓋 candidate/tree/hash drift、spec/design/text drift、wrong run/candidate、已派 verify、active job、unknown/unmerged source。
- [ ] **T4 Manager/Registry recovery transition**：新增或收斂一個 dedicated Manager recovery entrypoint。由 Manager 自行重算來源與 candidate binding；Registry 只接受 expected run/current candidate/current authority CAS，原子更新 baseline、audit receipt ref 與 verify pending state。是否新增 `recover-planning-baseline` action，須依 T0 API trace 定案。
- [ ] **T5 idempotency and fresh-run fallback**：測相同 receipt 重送是 no-op、不重複 dispatch；CAS drift 不派工。已 abandoned/superseded、source 不明或 candidate 不符時，Recovery 拒絕；`work start` 建立新 run 且不沿用舊 candidate/gate evidence。
- [ ] **T6 regression boundary**：保留現有 checkbox-only tolerance；新增 task 行、改標題、重編號、spec/design 修改仍 fail-closed。不得吸收 #937 builder task restructuring 或 #897 harvest/ancestor recovery。
- [ ] **T7 documentation, CLI, policy and delivery gates**：如採新 action，同步 `cortex run work --help` CLI help／operator docs；實作時補 R-09 fragment 與 CHANGELOG entry。執行 targeted tests、repo-required pytest/OpenSpec、帶 PR 上下文 policy check；各自報告 acceptance-plan evidence 與 runtime evidence。

## Completion boundary

Implementation is complete only when plan review freezes its exact accepted source before build, the exact same-run candidate path is auditable and idempotent, all unproven drift still fails closed, and the fresh-run fallback never adopts old-run evidence. This planning packet alone does not change runtime or complete #961.
