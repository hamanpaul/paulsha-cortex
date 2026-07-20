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
- [x] 1.6 保留ProjectState API一個release cycle；通過focused/full tests、help snapshots、OpenSpec、policy與preflight。

## 2. PR B — Persona workflow

- [x] 2.1 RED-testregistry v1 backup→v2 migration、legacy records、WorkflowRun/Step與claim-key restart idempotency。
- [x] 2.2 實作planner persona、`feature-oneshot` combo與Deck persona binding workflow manifest。
- [x] 2.3 RED-testaccepted artifact completeness、question pack、TBD/undecided marker與primary/secondary evidence integration。
- [x] 2.4 實作model identities與`agy` safe plan/sandbox launcher；unknown/same-domain/unavailable/malformed全部fail-closed。
- [x] 2.5 驗planner/builder/reviewer domain separation及brainstorm/ForeignReview gate分離。
- [x] 2.6 實作manifest per-card durable dispatch/resume、dispatch output baseline與run/card/Candidate-bound report gate、terminal canonical evidence、逐operation recoverable intent transaction、persisted planning authority CAS replacement，以及PermissionError仍可完整還原mode/xattrs/entries的planner sandbox rollback。
- [x] 2.7 通過migration/focused/full tests、help、OpenSpec、policy與preflight。
- [x] 2.8 RED-test並實作planning-authority seed、declared input snapshot/terminal hash重驗、versioned bounded card prompt與legacy same-phase input reconciliation。
- [x] 2.9 RED-test並修正dead headless job的operator stop：periodic只reconcile，explicit queued resume才可重派同一run/card。

## 3. PR C — Delivery automation

- [x] 3.1 RED-testmanual default、confirmed Todo+label auto claim、issue-only、missing issue、label removal與crash/restart。
- [x] 3.2 實作typed `work link|unlink|start|resume|auto` control requests與periodic auto-claim scan；Manager維持唯一writer；auto無issue selector時mutation全部mapped issues且API failure fail-closed。
- [x] 3.3 實作official archive、Todo/spec/docs/changelog gate、zh-TW PR metadata、policy與`PSC_PREFLIGHT_CMD` runner，包含exact-SHA push、remote reread與可恢復的新PR create orchestration。
- [x] 3.4 RED-testold-HEAD/error Copilot review、unresolved thread、push未重審、failed/cancelled/pending checks與HEAD race。
- [x] 3.5 實作每HEAD review epoch、兩輪/15分鐘bounded loop、exact-tree skip-tests與needs_human fallback。
- [x] 3.6 實作`gh pr merge --merge`前final reread、stable-hash atomic durable `merge-authorized` record、免重跑preflight的crash reconciliation與merge後remote closure/authority-bound CompletionRecord；V1多delivery target轉needs_human。
- [x] 3.7 通過GitHub seam/focused/full/integration tests、help、OpenSpec、policy與preflight；完成planner issue #5與ship-gate issue #16。Issue #8其餘task-type選牌、計分/成本與skill治理範圍保留，不由本change虛假關閉。
- [x] 3.8 RED-test並實作immutable exact-HEAD `maintainer-review` attestation、Copilot/maintainer二選一gate與保留實際review kind/ref/hash的merge authorization v2。
- [x] 3.9 RED-test並實作pre-binding target-cardinality stop恢復：operator補齊唯一PR/OpenSpec/Todo authority後explicit resume可重綁同一run journal；既有binding與其他needs_human仍fail-closed。
- [x] 3.10 RED-test並實作post-archive retry-build：只保留Manager-owned official archive authority，重開最後builder並讓新Candidate單調延伸；其他已通過ship card拒絕retry。
- [x] 3.11 RED-test並修正delivery GitHub pagination：不依賴本機gh不支援的`--slurp`，以typed JSONL page stream完整重讀checks/statuses/reviews，malformed page維持fail-closed。
- [x] 3.12 RED-test並修正既有PR的Candidate推送：fresh verify/review後先以PR context重跑乾淨exact-Candidate preflight，再由Manager冪等push並重讀授權feature ref，remote HEAD不符時不得進入delivery gate。
- [x] 3.13 RED-test並修正terminal closure provider：Todo以default revision精確綁定的Contents identity讀取，僅bounded retry HTTP 502/503/504，production ancestry compare只查canonical workflow-linked PR。
- [x] 3.14 RED-test並修正existing-PR metadata transaction：title/body PATCH、labels PUT及identity reread僅對HTTP 502/503/504 bounded retry；create/push/merge side effect維持單次fail-closed。
- [x] 3.15 RED-test並修正exact metadata冪等性：先authenticated reread title/body/labels，全部一致時不發PATCH/PUT；只有drift才write並再次完整reread。
- [x] 3.16 RED-test並修正delivery finding閉環：`fix-required`由trusted adapter fail-closed映射為`needs_human`，使operator可用exact-Candidate `retry-build`重開builder。
- [x] 3.17 RED-test並修正builder terminalization recovery：final builder job已成功退出（`exited/0`）但immutable evidence未綁定時，允許exact-Candidate `retry-build`在窄化build phase重派；真正failed job、前置build、job狀態與input snapshot維持fail-closed。
- [x] 3.18 RED-test並收緊plan/build terminal output prompt：`outputs`只允許符合manifest的repo-relative artifact path字串；manifest無outputs時明示固定為空陣列，禁止描述性物件造成合法repair Candidate無法綁定。
- [x] 3.19 RED-test並修正typed maintainer review recovery：只有完整path/hash且已由WorkflowRun綁定的exact-HEAD maintainer evidence可重入`copilot-*` needs-human stop；其他stop與錯誤binding維持fail-closed。
- [x] 3.20 RED-test並修正CompletionRecord typed delivery evidence：保留merge authorization實際使用的`copilot`或`maintainer-review` kind/ref/hash，要求兩者恰好一種，避免maintainer-authorized merge卡在remote closure。
- [x] 3.21 RED-test並新增`work abandon`：exact run ID CAS、current authority、actor/reason、active Job與pre-delivery side-effect gate全部fail-closed；成功只寫immutable evidence並將run設為superseded，不建立CompletionRecord。
- [x] 3.22 RED-test並修正post-merge closure routing：delivery journal完整綁定merged Candidate/merge commit/authorization時，resume略過已被official archive移走的active planning path重驗，仍由ship validator完整驗證CompletionRecord與remote closure。
- [x] 3.23 RED-test並修正terminal authority transition：merged/done closure接受immutable merge authorization保留pre-terminal WorkAuthority digest；merge-authorized與merge前gate仍要求current digest精確相等，tampered evidence維持fail-closed。
- [x] 3.24 RED-test並修正Completion Draft重試版控：以排除`completed_at`的closure語意hash建立immutable revision；相同語意重播沿用首份draft，default branch或authority前進則保留舊draft並建立新revision，malformed collision維持fail-closed。
- [x] 3.25 RED-test並修正CompletionRecord run-level evidence binding：驗證原始per-card verify/review canonical envelope後，以同一WorkflowRun ID派生closure evidence，讓CompletionRecord重新讀取時可同時精確綁定verification與ForeignReview。
- [x] 3.26 RED-test並修正post-archive repair的ship audit：Manager-owned archive job可綁定final Candidate的exact ancestor，前提是registry仍保存passed archive authority且Git ancestry成立；policy-commit仍要求exact final Candidate，unrelated commit維持fail-closed。

## 4. PR D — Bootstrap, docs, deployment, canary

- [x] 4.1 實作`cortex doctor --probe-live`的gh auth/permissions、label、preflight executable、model identities、agy smoke與service path probes。
- [x] 4.2 更新README、usage/help snapshots、service install與registry/snapshot migration docs。
- [x] 4.2a 統一interactive/service instance runtime discovery；installer保存`PSC_INSTANCE`，CLI control queue與Monitor client解析相同runtime roots/socket。
- [x] 4.2b 將brainstorm發布artifact從canonical evidence原子併入planning authority並固化發證revision；legacy resume只允許同evidence reconcile，缺evidence或dispatch exception恢復`needs_human`。
- [x] 4.2c 讓workflow的`commit_policy=required`及legacy fanout／dispatch／retry-build Codex builder在保留`workspace-write`下，透過明確commit capability只開放Git驗證出的current-worktree gitdir、shared objects、current-branch ref/reflog parent directories；清除inherited Git repository selectors，planner／verify／review不取得Git write directories，invalid metadata維持fail-closed。
- [x] 4.2d 讓schema/binding正確且明示`failed|needs_human`的plan/build workflow terminal只在explicit operator resume時重派同一run/card；舊job/log保留且periodic不自動重試。
- [x] 4.2e 讓sequential build card只把Candidate單調推進到目前Candidate的exact descendant worktree HEAD；verify/review仍維持exact equality。
- [x] 4.2f 讓verify/review只選明示review capability的foreign identity並在exact Candidate disposable clone以read-only launcher執行；原Candidate用tree snapshot防寫，Manager以phase專屬report root與durable publication journal擁有report/GateEvaluation binding，且舊planning-only Agy terminal只可由exact explicit operator recovery重派。
- [x] 4.2g 讓Claude reviewer以dontAsk、safe-mode及僅Bash工具面搭配fail-closed原生sandbox執行non-mutating tests；home/runtime sockets拒讀、Candidate deny-write、credential/MCP/remote隔離、缺bubblewrap/socat/srt或Unix-socket seccomp失效時拒絕啟動，且exact-bound無payload terminal只可在原始Candidate root精確等於Builder worktree時由explicit operator recovery重派。
- [ ] 4.3 在`paulsha-cortex`以低風險docs-only issue跑auto-label canary，刻意缺accepted plan以觸發異質brainstorm。
- [ ] 4.4 驗canary完整經brainstorm→build→ForeignReview→archive→preflight→typed maintainer current-HEAD review→merge commit→done；通過前不擴散auto label。
- [ ] 4.5 使用official CLI archive `unified-work-lifecycle`；issue #12只勾實際涵蓋項目。

## 5. Completion gates

- [x] 5.1 每個code PR更新changelog並通過`python3 -m pytest tests/ -q`、`openspec validate --all --strict`與`git diff --check`。
- [x] 5.2 每個code PR通過`python3 -m policy_check --repo .`與pinned-engine CI-parity `PSC_PREFLIGHT_CMD`。
- [x] 5.3 每個code PR完成independent review；最後一次fix後重審，Critical/Important findings為零。
- [x] 5.4 多worktree整合後重跑full integration suite，保存exact tree hash evidence。
