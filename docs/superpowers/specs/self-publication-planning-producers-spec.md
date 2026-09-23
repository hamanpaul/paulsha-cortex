---
status: accepted
work_item: self-publication-planning-producers
issue: 979
---

# feat(coordinator): 僅為本次 Manager planning publication mint receipt（#979）

## Boundary

- Parent: [#963](https://github.com/hamanpaul/paulsha-cortex/issues/963) 與 [#847](https://github.com/hamanpaul/paulsha-cortex/issues/847)。
- Hard dependencies: [#992](https://github.com/hamanpaul/paulsha-cortex/issues/992) freezes the receipt value and producer unions; [#993](https://github.com/hamanpaul/paulsha-cortex/issues/993) owns registry carry-forward, private append and full-registry CAS. Production work waits for both exact contracts to merge. #993 carries its own #966 CAS and #968 writer-coordination gates. #979 does not wait for umbrella #978 to merge, which would create a dependency cycle.
- work item: `self-publication-planning-producers`; sizing projection combo: `feature-oneshot` until root publishes a registered work/combo binding.
- This child connects only two Manager workspace planning producers: canonical brainstorm publication and accepted-plan card materialization. Production orchestration changes stay in `paulsha_cortex/coordinator/manager.py`; tests and docs may change as required.
- A positive receipt must come from this run's formal Manager acceptance and actual publication. Existing `planning_authority`, files, same bytes, source prefixes, caller-supplied rows and claim key alone never prove publication.

## Problem and Outcome

Parent #847 AC01/AC04 require verifiable accepted-publication provenance. Brainstorm currently records filesystem operations in `_PlanningPublicationTransaction`, but its peer-evidence artifact rows can include unchanged source artifacts. Plan-card materialization writes through the same helper with `journal_root=None`, then updates the registry, leaving a crash window between file creation and run-state persistence. The brainstorm recovery marker checks a gate ref without a self-publication receipt.

This child gives both producers the exact #992 receipt v1 value, with pre-mutation accepted-input evidence and a retained post-publication intent core. Each receipt is coupled to the matching authority/evidence/gate and run phase/step update through #993's private append seam in one registry snapshot commit. Recovery verifies the immutable sidecars, transaction-owned operations, claim-era state, receipt and file bytes. A surviving file without the required retained evidence never becomes provenance.

## Frozen #992 contract used by these producers

#992 is the only schema authority. #979 may construct receipts only from its exact typed value and closed producer unions; it must not add keys or infer alternative formulas.

- The exact receipt envelope keys are `schema, receipt_id, producer_kind, producer_event_id, publication_id, repo, work_id, run_id, claim_key, pre_publication_authority_sha256, accepted_input, acceptance_evidence, published_object, transaction`; `schema` is `cortex-self-publication-receipt/v1`. Recompute `receipt_id = H("cortex-self-publication-receipt/v1", envelope_without_receipt_id)`.
- Canonical bytes are `J(v) = UTF8(json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))`. `H(d,v) = lowercase_hex(SHA256(ASCII(d + LF) || J(v)))`; `raw_sha256(v) = lowercase_hex(SHA256(J(v)))`. Reject duplicate keys, non-finite values, invalid Unicode scalars, unknown keys and wrong types. Hashes are lowercase 64-hex.
- `pre_publication_authority_sha256` is the WorkAuthority digest bound to the current run/claim at trusted claim or refresh time. Because `apply_workflow_action` runs after `work_bridge` and has no live `WorkAuthority`, #979 may use `WorkflowRun.source_revision` only from the exact current persisted Manager-owned row (never caller `args` or a caller dictionary), after verifying repo/work/run/claim identity and `claim_key_for_authority_digest(repo=repo, work_id=work_id, authority_digest=source_revision) == claim_key`. Capture the full-registry raw-byte revision before the first sidecar/workspace mutation; keep the same digest/claim-key tuple through the operation and pass that held revision to #993. Any missing/untrusted proof, source_revision not lowercase 64-hex or key mismatch, intervening authority restart/refresh/reclaim, changed row binding or stale revision fails closed with no receipt. Do not resolve or reconstruct `WorkAuthority` in `manager.py`; a naked `source_revision` relabel is invalid.
- `transaction` has exact keys `kind, intent_ref, intent_sha256`; `intent_ref` is a normalized relative POSIX path under the configured coordinator state root and `intent_sha256` hashes the exact retained `J(intent_core)` bytes. Accepted-input snapshots and prepared intent cores are retained after commit; neither receipt depends on a mutable/retires-after-recovery journal. Planning `transaction.kind` equals its exact referenced intent-core schema: `cortex-planning-publication-intent/v1` for brainstorm and `cortex-plan-materialization-intent/v1` for plan. #992 rejects cross-variant mappings; no local alias is allowed.
- Each operation proof has exact keys `kind, path_domain, path, before_exists, before_sha256, after_sha256, mutation`. `kind` is `artifact` or `evidence`; `path_domain` is `workspace` or `coordinator_state`; `path` is normalized relative POSIX under that configured root. `before_sha256` is null only when `before_exists=false`; all other hashes are lowercase 64-hex. The proof omits rollback contents, modes, created directories and mutable journal state.
- Sidecar path segments use RFC3986 percent encoding (only ASCII letters, digits, `-._~` remain unescaped). Workspace refs and coordinator-state refs are separate domains; never resolve a receipt-provided absolute path.

## Producer requirements

### Brainstorm

1. Before any Manager publication mutation, retain an immutable accepted-input snapshot under `planning-input-snapshots/<encoded-run-id>/brainstorm/<define-attempt>/<encoded-pack-id>.json`. Capture the non-negative `run.attempts["define"]` value and exact `CompletenessReport.to_dict()`, default `QuestionPack.to_dict()`, validated accepted question-pack `to_dict()`, and ordered `input_artifacts` rows. Each input row carries `kind, ref, sha256, accepted, reasons, blocking_markers`; its SHA hashes the exact UTF-8 source bytes. Snapshot keys, schema `cortex-brainstorm-accepted-input/v1`, and `accepted_input={kind:"brainstorm_context/v1", ref, sha256}` follow #992 exactly; `sha256=raw_sha256(snapshot)`.
2. Manager captures the validated pack without changing `planning.py`: wrap the `primary_questioner` callback, validate the returned pack against the same report using the planning validator, retain `pack.to_dict()`, and return the original result to `run_heterogeneous_brainstorm`. In the `artifact_writer` callback, atomically persist/fsync the snapshot before calling `_publish_planning_artifacts`. If either capture or snapshot persistence fails, do not publish.
3. On ready, re-open and hash the exact peer evidence named by the returned `GateEvidenceRef(kind="brainstorm")`. `acceptance_evidence` exact keys are `kind, ref, sha256, facts`, with kind `brainstorm_peer/v1`. The evidence ref is `coordinator-state://` plus the encoded normalized path under the trusted coordinator state root; its `sha256` is SHA-256 of the exact file bytes. Derive `question_pack_id` from `payload.question_pack.pack_id`; require equality with the retained accepted pack ID. The exact `facts` projection is `schema_version, kind, scope, question_pack_id, secondary_evidence_hash, artifacts`; `scope` is `repo, work_id, source_revision`; each artifact row is `kind, ref, sha256`.
4. A peer-evidence artifact row is eligible only if it uniquely joins a same-transaction operation with `kind="artifact"`, `path_domain="workspace"`, `mutation=true`, matching canonical path/ref and `after_sha256 == row.sha256`. Semantic artifact kind comes from the evidence row. Unchanged/original report rows and non-unique joins mint no receipt. Match the row to exactly one declared output pattern; `published_object` keys are `kind, artifact_kind, ref, sha256, declared_output_pattern`, where kind is `planning_artifact` and artifact kind is `spec`, `design` or `plan`.
5. Brainstorm `event_basis` exact keys are `repo, work_id, run_id, claim_key, pre_publication_authority_sha256, accepted_input_ref, accepted_input_sha256, acceptance_evidence_ref, acceptance_evidence_sha256, question_pack_id, define_attempt`. Recompute `producer_event_id = H("cortex-self-publication-event/brainstorm/v1", intent_core.event_basis)` and cross-check every field against the envelope and both retained evidence records.
6. Brainstorm `intent_core` exact keys are `schema, producer_kind, repo, work_id, run_id, claim_key, pre_publication_authority_sha256, define_attempt, accepted_input, acceptance_evidence, event_basis, operations`; schema is `cortex-planning-publication-intent/v1`, producer kind is `brainstorm_artifact`. The operation list is sorted by `(path_domain, path, kind, after_sha256)`, with no duplicate `(path_domain, path)`. After artifact/evidence writes are durable, persist/fsync exact `J(intent_core)` before the registry commit. For each matched output, `publication_id = H("cortex-self-publication-publication/brainstorm/v1", {"producer_event_id": producer_event_id, "operation": matched_operation})`. One event may have multiple output receipts with distinct publication IDs.

### Accepted-plan materialization

1. Preserve the existing selection rule: use the first accepted `kind="plan"` row in `assess_planning_completeness(...).assessments` order. Do not claim there is only one accepted plan. `accepted_input` exact keys are `kind, ref, sha256`, with kind `planning_artifact`; ref is the selected source's canonical workspace-relative path, and sha256 hashes its exact UTF-8 bytes.
2. Freeze the complete ordered selection/assessment facts before target mutation. `acceptance_evidence` exact keys are `kind, ref, sha256, facts`, kind `manager_plan_acceptance/v1`. Facts exact keys are `selection_rule, ordered_assessments, selected_source, phase, card, phase_attempt, declared_output_pattern, matching_output_pattern_count`; selection rule is `first_accepted_kind_plan_in_assessment_order/v1`. Each ordered assessment has `kind, ref, sha256, accepted, reasons, blocking_markers`; blockers have `kind, line, text`; selected source has `kind, ref, sha256` and equals the first accepted plan row. `phase="plan"`; card is the current exact `WorkflowStep.card`; pattern count is integer `1`.
3. Do not invent a step ID. Require exactly one current `WorkflowStep` matching `(phase="plan", card=<selected card>)`, and a present non-negative integer `run.attempts["plan"]`. If zero/multiple steps match or attempt is missing/invalid, stop before writing the target. Persist/fsync the canonical pre-write snapshot under `planning-input-snapshots/<encoded-run-id>/plan/<phase-attempt>/<encoded-card>.json`; its exact keys are `schema, producer_kind, repo, work_id, run_id, claim_key, pre_publication_authority_sha256, phase, card, phase_attempt, accepted_input, acceptance_evidence_facts`, schema `cortex-plan-accepted-input/v1`, producer kind `plan_materialization`. `acceptance_evidence.ref` names this retained snapshot, and `acceptance_evidence.sha256=raw_sha256(snapshot)`; retain and revalidate the facts, not only the locator/hash. The `workflow-run://.../plan-acceptance` token is only a derived logical identity from `(run_id, phase, card, phase_attempt)`, not a serialized ref.
4. Require exactly one declared output pattern and one safe canonical target. The selected source bytes must still match the frozen input hash before write; use the existing no-clobber/CAS publication path, then re-read and hash the source and newly created target before commit. A pre-existing same-byte target is not a publication.
5. `published_object` has exact keys `kind, artifact_kind, ref, sha256, declared_output_pattern`, with `kind=planning_artifact`, `artifact_kind=plan`, and target ref/hash distinct from source. The plan `event_basis` exact keys are `repo, work_id, run_id, claim_key, pre_publication_authority_sha256, phase, card, phase_attempt, selected_source_ref, selected_source_sha256, acceptance_evidence_sha256`; `producer_event_id = H("cortex-self-publication-event/plan/v1", intent_core.event_basis)`.
6. Plan `intent_core` exact keys are `schema, producer_kind, repo, work_id, run_id, claim_key, pre_publication_authority_sha256, accepted_input, acceptance_evidence, event_basis, output_operation`; schema `cortex-plan-materialization-intent/v1`, producer kind `plan_materialization`. `output_operation` is the exact operation proof for the single new target (`kind=artifact`, `path_domain=workspace`, `mutation=true`, path/hash equal to target). Persist/fsync exact `J(intent_core)` after target durability and before registry commit. Compute `publication_id = H("cortex-self-publication-publication/plan/v1", {"producer_event_id": producer_event_id, "target": published_object})`.

### Shared append, recovery and ownership

- #979 validates sidecar bytes, evidence files, operation joins, current run/claim and target/source hashes before calling #993. Pass #993's expected full-registry raw-byte revision, exact run/work/repo/claim, strict #992 receipt and explicit same-row patch. Append the receipt with `planning_authority`, gate/evidence refs, source revision and phase/attempt/step state in one registry snapshot persist. #993 must reject stale CAS without merge/retry; on conflict, reconcile only the owned transaction and never mint against a stale run.
- The mutable recovery journal may carry expected receipt IDs in a separate marker outside the immutable core hash. It is not itself the permanent transaction proof; `transaction.intent_ref`/`intent_sha256` bind the retained immutable core. A receipt can be verified after journal retirement by reopening accepted-input/evidence/intent sidecars from the configured trusted state root.
- On fresh reload or persist-after-raise, committed means the exact valid receipt and the entire expected run patch are present and all retained sidecars, operations and bytes still match. Otherwise roll back only this transaction's unadopted output with matching after hash; changed/adopted files remain untouched. A crash before the prepared intent core is durably retained never mints from matching current bytes.
- Old v2/v3 planning journals remain readable by their existing recovery rules. Missing receipt on a legacy run is not backfilled; unknown/malformed rows or containers follow #992/#993 fail-closed behavior.

## Child Acceptance

- C01 Brainstorm positive path: same-run pre-write snapshot, ready peer evidence, unique exact mutation join and post-write intent core produce one strict #992 receipt per eligible output; fresh reload validates all retained hashes and exact claim-era binding.
- C02 Plan-copy positive path: exact first-selected source row, full ordered assessment facts, unique phase/card/attempt identity and new target operation produce one receipt; fresh reload validates source, target, receipt and run patch.
- C03 Formal start/intake foreign-artifact negative: all visible ref/hash/bytes and `planning_authority` may match, but no valid producer receipt exists.
- C04 Same bytes, source prefix, ref, pre-existing target, claim key and caller-supplied row alone do not mint. No output without an exact current mutation mints.
- C05 Inject restart before/after input snapshot, workspace mutation, evidence write, prepared intent core, append and registry persist; outcome is one exact commit/replay or fail-closed drift, never orphan-file proof.
- C06 Test same-byte existing target, changed/adopted target, exact replay, competing run/claim, source drift, stale #993 revision, persist failure, unknown/malformed/duplicate receipt, wrong operation and snapshot tampering.
- C07 Removing receipt identity, operation ownership, source/target hash, accepted-input snapshot or claim-era check makes its production-path negative control fail.
- C08 Existing v2/v3 transaction journals and old runs remain readable under their prior rules; no legacy record is upgraded into a valid receipt. Invalid row/container semantics come from #992/#993.

## Ownership and Non-goals

- #992 owns receipt envelope, canonical identity/digest, exact producer unions and legacy/unknown receipt value behavior. #993 owns registry carry-forward, private append, same-row patch, full-file CAS and persistence rollback. #979 consumes both contracts; it does not duplicate schema, storage, CAS or registry primitives.
- #980 owns PR publication only; coordinate shared `manager.py` hunks and keep PR create/read-back outside this child.
- #964 owns receipt-backed drift classification and workflow consumers. #979 does not change start/intake/resume, reset suppression, delivery/status/CLI, or consumer authority rules.
- #965 owns status/CLI projection and loaded-runtime AC10 canary. This child does not run or claim that canary.
- #847 AC01–AC10, aggregate sizing and completion gate stay fully in force. This child supplies planning producer evidence toward AC01/AC04; it closes neither #963 nor #978/#847.
- No PR create/GET/read-back, GitHub mutation, source-membership exclusion, broad restart policy, ACL/Trust Root/model authorization, runtime install or live operation is in scope.

## Parent acceptance: unchanged #847 AC01–AC10

The full parent checklist below remains unchanged and is still required for #847 completion. This child does not narrow or replace any parent AC.

## 機械驗收

- [ ] AC01：建立明確且版本化的「自身 frozen publication metadata 等價／真實 authority 變更／證據不足」分類。只有 exact repo/work/run/claim-era、已接受 frozen artifact、內容 hash、受治理 source owner／發布 provenance 全部可驗證，且其他需求、權限與 authority 成員均不變，才允許 metadata 等價；不得只按 source prefix、檔名、同 bytes 或 caller 自述決定。

- [ ] AC02：在正常 monitor ingestion → confirmed authority／claim → automatic action／Manager tick 的 production 路徑，注入自身已 frozen 內容延後被納入 source membership 的情境；原 needs_human 與理由、已接受 gates／evidence、candidate／verified head、attempt 計數、job／model invocation 數、既有 execution model binding 均不變，零額外 spawn。不得只測私有 helper、預造分類成功或把清 facet 當成正確接續。

- [ ] AC03：metadata 等價可由既有正式 resume／reconciliation consumer 安全消費，保留原 frozen authority／accepted evidence 的綁定語意與可重算 provenance。不能原地改寫舊 job、evidence 或 claim-era 來冒充相容，也不能只避開一次 reset 卻使後續 tick 因 digest 不合反覆 restart；合法 operator recovery 仍明示可用且不被自動觸發。

- [ ] AC04：缺少可信歷史 frozen snapshot、發布 receipt／provenance 缺失或不一致、未知 schema、同 bytes 但陌生 owner／另一 run／另一 work／非該 run 已接受產物等負例，均不可取得 metadata 豁免。不能從現有檔案反推歷史授權；保留受治理拒絕／人工處理或既有合法裁決，具體原因可見。

- [ ] AC05：真正內容、需求範圍、issue／PR／OpenSpec／todo 成員、owner／permission／Trust Root 或其他語意 authority 的變更，逐欄負例仍走既有合法 authority restart／精準 invalidation／換代政策；不得全域關閉 restart、剝除全部 planning source 或保留實際已失效的 gate。

- [ ] AC06：metadata 判定前後注入 authority／source owner／artifact bytes 漂移，以 exact-run／authority 比較與 CAS 或等效持久一致性機制拒絕或重裁決。相同 metadata 重送、兩個競爭 request、monitor 與 resume 交錯不得產生額外 attempt、雙 spawn、重寫 evidence 或放寬模型授權。

- [ ] AC07：分類前後及持久化邊界注入 crash/restart，既有 run／needs_human／accepted gates 與 metadata 決策可安全重建；重送冪等，無永久 digest mismatch 迴圈，延遲舊 terminal 不覆蓋現行 accepted state。缺證據不能在重啟後變成預設等價。

- [ ] AC08：decision／status／audit 明確區分 metadata 等價、真實 authority change 與缺證據；列出 old/new authority reference、source delta、被驗證的 frozen artifact／publication provenance 與保留／失效 stage、合法 next action，不輸出 secrets、不偽造 approval／passed evidence。旁觀者可查明為何沒有重跑或為何必須重裁決。

- [ ] AC09：新增正常 production 路徑的不變量守門與負控制；移除 provenance／owner／其他 authority 不變檢查，或讓等價事件落入 destructive reset，測試必須變紅。重驗 #524 in-flight 保護、#216 精準 invalidation、#776 自產 OpenSpec 與明示 recovery；對 #843／#844 只驗本票與其 recovery conformance／same-era reuse 契約邊界，未交付能力不得標 passed，不要求本票實作 conformance／reuse 引擎，也不要求等待兩票整包完成。全稱結論由 runtime-path invariant test 維持，靜態掃描僅作當下佐證。

- [ ] AC10：正式 source／tests／documentation／CLI status 的 producer→consumer 接線均交付；已載入修正版 runtime 的受治理 canary 記錄 runtime identity、同內容 source-membership 事件、前後 needs_human／gates／attempt／job／model binding 與零額外 spawn。既有資料須相容讀取、未知／缺欄位 fail closed。未實作、source test、merge、installed 與 live 證據分開，不以 unit test 取代 live 驗收。

## Official child sizing and planning completeness (2026-09-24)

The repo helper was run on 2026-09-24 against the three exact rows below and `feature-oneshot`, the issue-body combo projection. `current_sizing_snapshot` returned `(6, "yellow")`; `compute_sizing_score` returned dimensions `(0, 2, 2, 0, 2)` and total 6. `assess_planning_completeness` returned `complete=True`, no missing kinds, and all three artifacts accepted with no reasons or blocking markers. This is a comparison snapshot: no registered selected-combo binding was found for the child, so root must publish that binding and rerun before intake or after any artifact edit.

| Dimension | Score | Evidence / calculation |
|---|---:|---|
| `domain_breadth` | 0 | Production source remains one module: `manager.py`. |
| `state_consistency` | 2 | Workspace outputs, retained pre-write and prepared-intent sidecars, registry CAS/append and restart reconciliation cross durable boundaries. |
| `acceptance_surfaces` | 2 | `feature-oneshot` gate spine has 4 entries; applicable R-09/R-16/R-19 add 3, signal 7 maps to 2. |
| `spec_stability` | 0 | Mechanical `stability-risk-v2` result from accepted spec/design/todo, if all are accepted and no planning kind is missing. |
| `orchestration` | 2 | The combo has 11 cards and 11 persona bindings. |
| **Total / band** | **6 / Yellow** | `current_sizing_snapshot=(6, "yellow")`; this does not replace #847 full-scope sizing. |

Artifact rows: `spec=docs/superpowers/specs/self-publication-planning-producers-spec.md`, `design=docs/superpowers/specs/self-publication-planning-producers-design.md`, `plan=docs/superpowers/workstreams/self-publication-planning-producers/todo.md`. This child score never replaces #847's full-scope sizing or AC01–AC10 gate.

## Source anchors

Implementation anchors on the inspected checkout: `paulsha_cortex/coordinator/manager.py` — `_PlanningPublicationTransaction`, `_publish_planning_artifacts`, `apply_workflow_action` brainstorm path, `_materialize_plan_card_output` and `_dispatch_workflow_card`; `paulsha_cortex/coordinator/planning.py` — `run_heterogeneous_brainstorm`, `validate_question_pack` and `assess_planning_completeness` as read-only producer contracts. #979 production changes stay in `manager.py`; re-read source after #992/#993 merge before coding.
