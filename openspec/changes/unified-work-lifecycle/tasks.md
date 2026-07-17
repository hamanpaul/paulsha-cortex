> Exact files、RED/PASS commands與commit boundaries以`docs/superpowers/plans/2026-07-17-unified-work-lifecycle.md`為準。本檔只追蹤OpenSpec apply進度。

## 0. Planning baseline

- [x] 0.1 確認umbrella issue #14、關聯issue #4/#5/#8/#10/#12與scope boundary；不建立重複issue。
- [x] 0.2 完成CONTEXT glossary、ADR、accepted source spec、implementation plan與OpenSpec validation。
- [x] 0.3 修正issue #4 transient scan/interval，加入scan health regression tests與changelog。
- [x] 0.4 移除`dispatch-discipline-improve` stale active copy、保留正式archive，並以merged PR #11/remote archive證據核對issue #10。

## 1. PR A — Monitor truth model

- [x] 1.1 RED-test GitHub/repo/workflow providers、archive exclusion、scan race、auth/rate-limit/timeout與restart cache。
- [x] 1.2 實作`WorkSource`/`WorkItem`、provider snapshots與`work-items-snapshot/v1` atomic durable store。
- [x] 1.3 RED-test並實作lifecycle reducer、reopen、strict closure與degraded freeze。
- [x] 1.4 RED-test並實作`.cortex/work-items.yaml`、`work_item` frontmatter、confirmed/inferred correlation、collision/exclusion與explain trace。
- [x] 1.5 實作`cortex list`、`cortex work show`、`cortex-work/v1` serializer與read-only socket APIs/subscription。
- [ ] 1.6 保留ProjectState API一個release cycle；通過focused/full tests、help snapshots、OpenSpec、policy與preflight。

## 2. PR B — Persona workflow

- [x] 2.1 RED-testregistry v1 backup→v2 migration、legacy records、WorkflowRun/Step與claim-key restart idempotency。
- [x] 2.2 實作planner persona、`feature-oneshot` combo與Deck persona binding workflow manifest。
- [x] 2.3 RED-testaccepted artifact completeness、question pack、TBD/undecided marker與primary/secondary evidence integration。
- [x] 2.4 實作model identities與`agy` safe plan/sandbox launcher；unknown/same-domain/unavailable/malformed全部fail-closed。
- [x] 2.5 驗planner/builder/reviewer domain separation及brainstorm/ForeignReview gate分離。
- [x] 2.6 實作manifest per-card durable dispatch/resume、dispatch output baseline與run/card/Candidate-bound report gate、terminal canonical evidence、逐operation recoverable intent transaction、persisted planning authority CAS replacement，以及PermissionError仍可完整還原mode/xattrs/entries的planner sandbox rollback。
- [ ] 2.7 通過migration/focused/full tests、help、OpenSpec、policy與preflight。

## 3. PR C — Delivery automation

- [x] 3.1 RED-testmanual default、confirmed Todo+label auto claim、issue-only、missing issue、label removal與crash/restart。
- [x] 3.2 實作typed `work link|unlink|start|resume|auto` control requests與periodic auto-claim scan；Manager維持唯一writer；auto無issue selector時mutation全部mapped issues且API failure fail-closed。
- [ ] 3.3 實作official archive、Todo/spec/docs/changelog gate、zh-TW PR metadata、policy與`PSC_PREFLIGHT_CMD` runner。（change-specific validation/archive/preflight與mapped PR authenticated update+reread已落地；新 PR create orchestration尚未完成。）
- [x] 3.4 RED-testold-HEAD/error Copilot review、unresolved thread、push未重審、failed/cancelled/pending checks與HEAD race。
- [x] 3.5 實作每HEAD review epoch、兩輪/15分鐘bounded loop、exact-tree skip-tests與needs_human fallback。
- [x] 3.6 實作`gh pr merge --merge`前final reread、stable-hash atomic durable `merge-authorized` record、免重跑preflight的crash reconciliation與merge後remote closure/authority-bound CompletionRecord；V1多delivery target轉needs_human。
- [ ] 3.7 通過GitHub seam/focused/full/integration tests、help、OpenSpec、policy與preflight；對應完成issue #5/#8與ship-gate issue。

## 4. PR D — Bootstrap, docs, deployment, canary

- [x] 4.1 實作`cortex doctor --probe-live`的gh auth/permissions、label、preflight executable、model identities、agy smoke與service path probes。
- [x] 4.2 更新README、usage/help snapshots、service install與registry/snapshot migration docs。
- [ ] 4.3 在`paulsha-cortex`以低風險docs-only issue跑auto-label canary，刻意缺accepted plan以觸發異質brainstorm。
- [ ] 4.4 驗canary完整經brainstorm→build→ForeignReview→archive→preflight→Copilot→merge commit→done；通過前不擴散auto label。
- [ ] 4.5 使用official CLI archive `unified-work-lifecycle`；issue #12只勾實際涵蓋項目。

## 5. Completion gates

- [ ] 5.1 每個code PR更新changelog並通過`python3 -m pytest tests/ -q`、`openspec validate --all --strict`與`git diff --check`。
- [ ] 5.2 每個code PR通過`python3 -m policy_check --repo .`與pinned-engine CI-parity `PSC_PREFLIGHT_CMD`。
- [ ] 5.3 每個code PR完成independent review；最後一次fix後重審，Critical/Important findings為零。
- [ ] 5.4 多worktree整合後重跑full integration suite，保存exact tree hash evidence。
