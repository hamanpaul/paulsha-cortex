---
status: accepted
work_item: self-publication-receipt-append
issue: 993
---

# Self-publication receipt registry append 設計（Child B，issue #993）

## Decisions

### D1 — Registry is the append authority

The typed model is frozen by Child A. Registry code alone owns the durable row, request-boundary rejection, carry-forward and private append. A caller cannot manufacture provenance by submitting a structurally valid JSON envelope; only the internal producer call path plus verified side-effect evidence can invoke append.

### D2 — Raw-byte full-registry CAS is mandatory

Every append runs through #966's exact whole-file revision CAS under the canonical transaction lock. The expected token is the hash of the raw registry bytes observed by the caller. A stale writer gets a conflict and its memory is restored; this method does not merge or silently retry. #862's slice-level binding CAS is separate and insufficient. #967's Manager-lifetime lock is also not a substitute for protection of all registry writers.

### D3 — Batch receipts and one-row commit boundary

A private append accepts a non-empty sequence of strict typed receipts from one producer kind/event ID and one producer patch for one WorkflowRun. It revalidates every receipt against the same expected full-registry raw-byte revision and exact run/work/repo/claim row, then appends all receipts and applies the patch in exactly one `_persist()` attempt. This is required for one brainstorm event that publishes several eligible artifacts; a loop of single-receipt commits is not equivalent. A batch is either wholly new or an exact full-batch replay with the identical patch. Partial prior presence, duplicate pair/receipt/object keys within the batch, or any conflict fails before persistence.

Filesystem/remote writes must already have their own owner-controlled durable intent/result journal. The registry does not attempt a distributed transaction. #979/#980 validate the external operation before calling append; registry checks only typed receipts' internal hashes, exact row binding and same-row patch consistency.

### D4 — Quarantine duplicate-key receipt JSON losslessly

`registry.py` must decode the registry with an object-pairs-preserving parser before constructing dictionaries. Duplicate keys or non-finite JSON values outside a receipt list row fail normal registry validation. Inside a `publication_receipts` list row, duplicate-key/non-finite syntax is quarantined as an opaque invalid row so other workflow rows remain readable; it never reaches Child A as a candidate valid receipt.

The quarantine is serialized as this reserved, JSON-safe one-key object (exact keys shown). `reason_code` is exactly `duplicate_json_member` or `non_finite_json_value`; if both occur, choose `duplicate_json_member` deterministically:

    {"$cortex_invalid_publication_receipt_v1": {
      "reason_code": "duplicate_json_member",
      "source_sha256": "<lowercase SHA-256 of raw UTF-8 row token>",
      "raw_utf8_base64": "<standard padded base64 of exact raw UTF-8 row token>"
    }}

The registry loader obtains the exact raw token span for each receipt row from the pair-preserving parser. For duplicate members, the raw token is authoritative; its ordered recursive pairs can be reconstructed on demand. The wrapper itself is the persisted diagnostic representation, not the original row shape and not a trusted receipt shape. On reload, require exact wrapper keys, strict base64 whose canonical re-encoding is byte-for-byte identical, matching raw-token hash, and a fresh parse that confirms the stated duplicate/non-finite reason; then restore the same opaque diagnostic. On ordinary `WorkflowRun.to_dict()`/registry writes, emit the same wrapper fields with the base64 token unchanged via deterministic canonical JSON serialization. This preserves the offending token bytes inside a canonical wrapper; it does not claim that the original duplicate-key object remains at that same JSON location. A user-provided object with this reserved key is always treated as invalid diagnostic data, never receipt authority. Other invalid JSON-compatible rows remain their exact raw JSON values plus stable diagnostic. A non-list invalid-container remains opaque and blocks append.

This boundary does not promise byte-for-byte preservation of the entire registry file; it preserves the offending row token and diagnostic. The source registry revision is still governed by #966 CAS. Append refuses any history whose duplicate/opaque contents prevent proving replay and uniqueness safely.

### D5 — Official request path is the negative oracle

The formal start/intake route constructs artifact rows from WorkAuthority-mapped refs and passes them to the internal workflow starter. Test this route with a pre-existing foreign file mapped by source revisions; do not claim a request field injects the private inner parameter. Exact matching planning_authority and bytes after fresh reload are deliberately insufficient without a producer receipt.

## Validation matrix

| Case | Expected |
|---|---|
| ordinary update after typed receipt | same receipt retained after reload |
| ordinary update with receipt replacement/removal | ignored or rejected; prior state retained |
| request start/intake with receipt field | no receipt injection |
| formal mapped foreign file start/intake | exact authority/hash present, no valid receipt |
| append exact full-batch replay | existing committed receipt set and identical patch, no rewrite |
| same event, several new publication IDs | all outputs appended in one persist with one patch |
| partial pre-existing batch or conflicting pair/publication/object | conflict, no mutation |
| stale whole-file revision | #966 CAS conflict, memory restored |
| concurrent second process | no lost first receipt, visible stale conflict |
| #862 CAS only / #967 lock only | insufficient; do not satisfy precondition |
| duplicate-key/non-finite receipt row | exact row token and hash preserved in reserved diagnostic wrapper; unrelated runs load |
| invalid container/ambiguous history | read okay, append blocked |
| failed persist | durable and in-memory old snapshot, no receipt |

## Five-dimensional sizing

Projection from current feature-oneshot combo: domain_breadth=0 (one production module, registry.py); state_consistency=2 (multiwriter raw-byte CAS, coupled append, rollback/reload); acceptance_surfaces=2 (gate spine 4 + applicable R-09/R-16/R-19); spec_stability=0 (accepted triad); orchestration=2 (11 cards, 11 persona bindings); total 6/Yellow. Recompute with the registered issue/work binding before dispatch.
