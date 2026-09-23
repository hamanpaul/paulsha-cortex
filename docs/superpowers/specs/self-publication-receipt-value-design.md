---
status: accepted
work_item: self-publication-receipt-value
issue: 992
---

# Self-publication receipt value 設計（Child A，issue #992）

## Decisions

### D1 — Parse for compatibility, validate strictly for trust

WorkflowRun.from_dict must remain able to load old rows and malformed receipt rows so unrelated run inspection works. A separate strict parser returns a typed immutable PublicationReceipt only for exact known v1. Unknown/malformed values remain opaque and visibly invalid; no loader guesses provenance or backfills from filesystem/PR metadata.

### D2 — Canonical facts and hash domains

The spec's J, H, and raw_sha256 are the only encodings. Each producer supplies exact immutable facts and a producer-specific event basis. receipt_id commits the complete canonical envelope. Intent sidecar bytes equal J(intent_core) exactly; its raw SHA and ref are stored in the receipt. The sidecar is retained after commit; the receipt never depends on a mutable/retired journal. Mutable phase, result and expected receipt ID marker are outside intent_core.

`pre_publication_authority_sha256` is not trusted merely because it is a 64-hex string or equals `WorkflowRun.source_revision`. A producer without a live `WorkAuthority` may use the latter only from the exact current persisted Manager-owned run row after checking the row's repo/work/run/claim identity and the deterministic claim-key binding for that digest. It freezes that pair before its first external mutation and holds the same whole-registry revision through Child B append; any authority restart/refresh/reclaim or claim-pair change after capture makes the append stale and produces no receipt. The value parser does not assert this provenance; #979 and #980 enforce it at their mint boundaries without adding envelope keys or expanding the Manager owner beyond trusted run proof.

The immutable evidence model uses two durable planning records to respect when facts become known. Planning transaction `kind` equals the exact `intent_core.schema` string (`cortex-planning-publication-intent/v1` for brainstorm; `cortex-plan-materialization-intent/v1` for plan), so there is no third alias to drift. The PR variant keeps `manager_pr_intent/v1`. For plan selection, the `workflow-run://.../plan-acceptance` URI is a derived logical identity from run/phase/card/attempt, not a serialized ref; `acceptance_evidence.ref` points only to the retained pre-mutation snapshot path.
- Before any workspace mutation, an accepted-input snapshot retains brainstorm CompletenessReport/question-pack facts or plan ordered selection/assessment facts.
- After outputs and peer evidence are durable, but before registry commit, a prepared intent core binds the snapshot ref/hash, evidence ref/hash and bounded operation proof. Both records remain after commit; neither uses mutable journal phase or rollback bytes as a permanent hash target.
- Plan-card selection uses the first accepted kind=plan assessment in current order; the retained snapshot and intent core record the entire ordered selection and assessment facts, not only a synthetic ref/hash.

### D3 — Event identity differs from output identity

A producer event may publish several rows. Its producer_event_id identifies the acceptance action and may be shared across those rows. Each object has a separate publication_id derived from event ID plus exact operation/target. Storage uniqueness is (producer_event_id, publication_id); event ID alone is not a conflict. Receipt ID and publication ID collision rules remain strict. A second pair cannot claim the same output ref in the same run/claim era.

### D4 — PR marker correlates; confirmed POST response proves create path

The public marker binds the PR to one immutable intent. It is not a credential, actor proof or evidence that a PR was created by Manager. A PR receipt requires a durable successful POST result with exact repository/number/REST id/node_id followed by a GET that returns the same remote identity and required open/head/base/marker facts. If the POST response is missing, timed out, or not durably recorded before crash, a matching GET remains ambiguous and no receipt is minted. Child C returns observed facts only; #980 joins those facts with #982's structured create witness.

### D5 — Value layer ends before registry ownership

The dataclass/serialization change belongs to workflow.py. Registry code reconstructs runs manually; therefore A cannot claim ordinary-update preservation. Child B owns every registry writer/carry-forward path, private append, whole-file revision CAS and persistence rollback. This boundary keeps A at one production module and gives B full test ownership for fresh registry reload.

## Validation matrix

| Case | Required result |
|---|---|
| exact v1 value, all hashes recompute | typed valid receipt |
| unknown schema, extra/missing union key, wrong type | opaque invalid row, stable diagnostic |
| duplicate JSON key / NaN / non-JSON value | rejected as invalid, never trusted |
| missing field in legacy run | empty legacy collection |
| malformed row in list | that row remains raw/invalid; other rows still load |
| non-list top-level receipt value | preserved invalid-container; not normalized |
| nested raw JSON mutated after copy | stored raw value unchanged |
| brainstorm report/pack facts changed | receipt digest or input fact validation fails |
| plan selection order/assessment changed | acceptance digest/intent fails |
| same event, distinct publication ID | allowed distinct outputs |
| same output with new event pair in same run/claim | conflict |
| PR marker without durable successful POST witness | no created receipt |

## Five-dimensional sizing

feature-oneshot helper projection: domain_breadth=0 (only production module workflow.py), state_consistency=1 (persistent WorkflowRun field and legacy serialization boundary; no cross-object transaction), acceptance_surfaces=2 (gate spine 4 + applicable R-09/R-16/R-19), spec_stability=0 (accepted triad), orchestration=2 (11 cards and 11 persona bindings), total 5/Yellow. The future child issue is not yet created or bound to a registered work item; this is a combo projection only.
