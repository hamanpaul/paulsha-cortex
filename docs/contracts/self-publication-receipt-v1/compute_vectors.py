#!/usr/bin/env python3
"""Independent stdlib-only golden-vector calculator for appendix.md.

This deliberately imports no paulsha_cortex code. It uses hashlib/json directly
so expected values do not depend on the production implementation under review.
"""

from __future__ import annotations

import hashlib
import fnmatch
import json
import re
import unicodedata
from pathlib import Path


OUT = Path(__file__).with_name("golden-vectors.json")


def j(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def h(domain: str, value: object) -> str:
    return sha(domain.encode("ascii") + b"\n" + j(value))


def obj(value: object) -> dict[str, object]:
    return {"value": value, "canonical_utf8": j(value).decode("utf-8")}


def raw_bytes(data: bytes) -> dict[str, object]:
    return {"utf8": data.decode("utf-8"), "sha256": sha(data)}


def structured(domain: str, value: object) -> dict[str, object]:
    return {"domain": domain, **obj(value), "sha256": h(domain, value)}


repo = "example-org/demo"
work_id = "work-contract-01"
run_id = "run-contract-01"
authority_sha = "a" * 64
brain_source_revision = "0123456789abcdef0123456789abcdef01234567"
claim_key = "claim:v1:" + sha(
    j({"repo": repo, "work_id": work_id, "authority_digest": authority_sha})
)

# --- brainstorm_artifact -------------------------------------------------
# Reproduce planning.py's deterministic default pack without importing production:
# _make_question hashes {kind,prompt,source_refs}; pack ID hashes question rows.
brain_plan_text = "---\nstatus: accepted\n---\n\n# Tasks\n- [ ] Keep the plan fixture.\n"
brain_plan_sha = sha(brain_plan_text.encode("utf-8"))
brain_plan_ref = "workstreams/demo/todo.md"
brain_questions = []
for missing_kind in ("spec", "design"):
    question_kind = f"missing-{missing_kind}"
    prompt = (
        "What authoritative content is required to create an accepted "
        f"{missing_kind}?"
    )
    identity = {
        "kind": question_kind,
        "prompt": prompt,
        "source_refs": [brain_plan_ref],
    }
    brain_questions.append(
        {
            "question_id": "q-" + sha(j(identity))[:16],
            **identity,
        }
    )
question_pack = {
    "schema_version": 1,
    "pack_id": "qp-" + sha(j(brain_questions))[:24],
    "questions": brain_questions,
}
brain_outputs = [
    {
        "kind": "spec",
        "ref": "docs/superpowers/specs/contract-1029-spec.md",
        "sha256": sha(
            b"---\nstatus: accepted\n---\n\n# Specification\n\n"
            b"## Requirements\n\nDefine receipt identity and validation requirements.\n"
        ),
        "content": (
            "---\nstatus: accepted\n---\n\n# Specification\n\n"
            "## Requirements\n\nDefine receipt identity and validation requirements.\n"
        ),
        "output_pattern": "docs/superpowers/specs/*contract-1029*-spec.md",
    },
    {
        "kind": "design",
        "ref": "docs/superpowers/specs/contract-1029-design.md",
        "sha256": sha(
            b"---\nstatus: accepted\n---\n\n# Design\n\n"
            b"## Decisions\n\nDescribe the component boundary.\n"
        ),
        "content": (
            "---\nstatus: accepted\n---\n\n# Design\n\n"
            "## Decisions\n\nDescribe the component boundary.\n"
        ),
        "output_pattern": "docs/superpowers/specs/*contract-1029*-design.md",
    },
]
assert all(fnmatch.fnmatch(row["ref"], row["output_pattern"]) for row in brain_outputs)
brain_snapshot = {
    "schema": "cortex-brainstorm-accepted-input/v1",
    "repo": repo,
    "work_id": work_id,
    "run_id": run_id,
    "claim_key": claim_key,
    "pre_publication_authority_sha256": authority_sha,
    "define_attempt": 1,
    "completeness_report": {
        "complete": False,
        "missing_kinds": ["spec", "design"],
        "artifacts": [
            {
                "kind": "plan",
                "ref": brain_plan_ref,
                "accepted": True,
                "reasons": [],
                "blocking_markers": [],
            }
        ],
    },
    "default_question_pack": question_pack,
    "accepted_question_pack": question_pack,
    "artifacts": [
        {
            "kind": "plan",
            "ref": brain_plan_ref,
            "sha256": brain_plan_sha,
            "content_utf8": brain_plan_text,
        }
    ],
}
brain_snapshot_ref = "planning-input-snapshots/run-contract-01/brainstorm/1.json"
brain_snapshot_bytes = j(brain_snapshot)
brain_snapshot_sha = sha(brain_snapshot_bytes)

secondary_evidence = {
    "schema_version": 1,
    "question_pack_id": question_pack["pack_id"],
    "evidence": [
        {
            "question_id": question["question_id"],
            "claims": [f"An accepted {question['kind'][8:]} artifact is required."],
            "source_refs": [brain_plan_ref],
        }
        for question in brain_questions
    ],
}
secondary_evidence_sha = sha(j(secondary_evidence))
brain_output_rows = [
    {"kind": row["kind"], "path": row["ref"], "content": row["content"]}
    for row in brain_outputs
]
brain_resolutions = [
    {
        "question_id": question["question_id"],
        "decision": f"Add the accepted {output['kind']} artifact.",
        "artifact_kind": output["kind"],
        "artifact_refs": [output["ref"]],
    }
    for question, output in zip(brain_questions, brain_outputs, strict=True)
]
brain_evidence_payload = {
    "schema_version": 1,
    "kind": "brainstorm-peer",
    "scope": {
        "repo": repo,
        "work_id": work_id,
        "source_revision": brain_source_revision,
    },
    "question_pack": question_pack,
    "secondary_identity": {
        "executor": "agy",
        "model_id": "fixture-model",
        "independence_domain": "external-fixture",
    },
    "secondary_evidence": secondary_evidence,
    "secondary_evidence_hash": secondary_evidence_sha,
    "primary_integration": {
        "schema_version": 1,
        "question_pack_id": question_pack["pack_id"],
        "secondary_evidence_hash": secondary_evidence_sha,
        "resolutions": brain_resolutions,
        "artifacts": brain_output_rows,
    },
    "artifacts": sorted(
        [
            {
                "kind": "plan",
                "ref": brain_plan_ref,
                "sha256": brain_plan_sha,
            },
            *[
                {"kind": row["kind"], "ref": row["ref"], "sha256": row["sha256"]}
                for row in brain_outputs
            ],
        ],
        key=lambda row: (row["kind"], row["ref"]),
    ),
}
brain_evidence_bytes = j(brain_evidence_payload) + b"\n"
brain_evidence_sha = sha(brain_evidence_bytes)
brain_evidence_abs_ref = "/fixture/coordinator/evidence/brainstorm/run-contract-01.json"
brain_gate_ref = {
    "kind": "brainstorm",
    "ref": brain_evidence_abs_ref,
    "sha256": brain_evidence_sha,
}
brain_acceptance = {
    "gate_ref": brain_gate_ref,
    "question_pack_id": question_pack["pack_id"],
}
brain_artifact_operations = [
    {
        "path_domain": "workspace",
        "ref": output["ref"],
        "kind": "artifact",
        "before_exists": False,
        "before_sha256": None,
        "after_sha256": output["sha256"],
        "mutation": True,
    }
    for output in brain_outputs
]
brain_evidence_operation = {
    "path_domain": "coordinator",
    "ref": "evidence/brainstorm/run-contract-01.json",
    "kind": "evidence",
    "before_exists": False,
    "before_sha256": None,
    "after_sha256": brain_evidence_sha,
    "mutation": True,
}
brain_operations = sorted(
    [*brain_artifact_operations, brain_evidence_operation],
    key=lambda row: (
        row["path_domain"], row["ref"], row["kind"], row["after_sha256"]
    ),
)
brain_intent = {
    "schema": "cortex-planning-publication-intent/v1",
    "repo": repo,
    "work_id": work_id,
    "run_id": run_id,
    "claim_key": claim_key,
    "pre_publication_authority_sha256": authority_sha,
    "accepted_input": {"ref": brain_snapshot_ref, "sha256": brain_snapshot_sha},
    "acceptance_evidence": {
        "ref": brain_evidence_abs_ref,
        "sha256": brain_evidence_sha,
        "question_pack_id": question_pack["pack_id"],
    },
    "operations": brain_operations,
}
brain_intent_bytes = j(brain_intent)
brain_intent_sha = sha(brain_intent_bytes)
brain_event_basis = {
    "repo": repo,
    "work_id": work_id,
    "run_id": run_id,
    "claim_key": claim_key,
    "pre_publication_authority_sha256": authority_sha,
    "accepted_input_sha256": brain_snapshot_sha,
    "brainstorm_evidence": {
        "ref": brain_evidence_abs_ref,
        "sha256": brain_evidence_sha,
    },
    "question_pack_id": question_pack["pack_id"],
}
brain_event_id = h("cortex-self-publication-event/brainstorm/v1", brain_event_basis)
brain_publication_bases = [
    {"producer_event_id": brain_event_id, "operation": operation}
    for operation in brain_artifact_operations
]
brain_publication_ids = [
    h("cortex-self-publication-publication/brainstorm/v1", basis)
    for basis in brain_publication_bases
]
brain_intent_ref = (
    f"planning-publication-intents/run-contract-01/brainstorm/{brain_event_id}.json"
)
brain_receipts = []
for output, publication_id in zip(brain_outputs, brain_publication_ids, strict=True):
    published = {
        "kind": output["kind"],
        "ref": output["ref"],
        "sha256": output["sha256"],
        "output_pattern": output["output_pattern"],
    }
    without_id = {
        "schema": "cortex-self-publication-receipt/v1",
        "producer_kind": "brainstorm_artifact",
        "producer_event_id": brain_event_id,
        "publication_id": publication_id,
        "repo": repo,
        "work_id": work_id,
        "run_id": run_id,
        "claim_key": claim_key,
        "pre_publication_authority_sha256": authority_sha,
        "accepted_input": {"ref": brain_snapshot_ref, "sha256": brain_snapshot_sha},
        "acceptance_evidence": brain_acceptance,
        "published_object": published,
        "transaction": {
            "kind": "cortex-planning-publication-intent/v1",
            "intent_ref": brain_intent_ref,
            "intent_sha256": brain_intent_sha,
        },
    }
    receipt_id = h("cortex-self-publication-receipt/v1", without_id)
    brain_receipts.append({"receipt_id": receipt_id, **without_id})
brain_publication_basis = brain_publication_bases[0]
brain_publication_id = brain_publication_ids[0]
brain_artifact_operation = brain_artifact_operations[0]
brain_output_text = brain_outputs[0]["content"]
brain_output_sha = brain_outputs[0]["sha256"]
brain_published = brain_receipts[0]["published_object"]
brain_receipt = brain_receipts[0]
brain_receipt_without_id = {
    key: value for key, value in brain_receipt.items() if key != "receipt_id"
}
brain_receipt_id = brain_receipt["receipt_id"]

# --- plan_materialization -----------------------------------------------
plan_rejected_text = "---\nstatus: draft\n---\n\n# Tasks\n- [ ] Draft only.\n"
plan_accepted_text = (
    "---\nstatus: accepted\n---\n\n# Tasks\n- [ ] Freeze receipt wire schema.\n"
)
plan_rejected_sha = sha(plan_rejected_text.encode("utf-8"))
plan_source_sha = sha(plan_accepted_text.encode("utf-8"))
plan_pattern = "docs/superpowers/plans/*receipt-contract.md"
plan_target_ref = "docs/superpowers/plans/receipt-contract.md"
plan_assessments = [
    {
        "kind": "plan",
        "ref": "workstreams/demo/draft-plan.md",
        "sha256": plan_rejected_sha,
        "content_utf8": plan_rejected_text,
        "accepted": False,
        "reasons": ["status-not-accepted"],
        "blocking_markers": [],
    },
    {
        "kind": "plan",
        "ref": "workstreams/demo/accepted-plan.md",
        "sha256": plan_source_sha,
        "content_utf8": plan_accepted_text,
        "accepted": True,
        "reasons": [],
        "blocking_markers": [],
    },
]
plan_snapshot_ref = (
    "planning-input-snapshots/run-contract-01/plan/2/planning-contract.json"
)
plan_snapshot = {
    "schema": "cortex-plan-accepted-input/v1",
    "repo": repo,
    "work_id": work_id,
    "run_id": run_id,
    "claim_key": claim_key,
    "pre_publication_authority_sha256": authority_sha,
    "phase": "plan",
    "card": "planning-contract",
    "phase_attempt": 2,
    "selection_rule": "first_accepted_kind_plan_in_assessment_order/v1",
    "assessments": plan_assessments,
    "selected_index": 1,
    "declared_output_patterns": [plan_pattern],
    "target": {
        "kind": "plan",
        "ref": plan_target_ref,
        "sha256": plan_source_sha,
        "output_pattern": plan_pattern,
        "match_count": 1,
    },
}
plan_snapshot_bytes = j(plan_snapshot)
plan_snapshot_sha = sha(plan_snapshot_bytes)
plan_accepted_input = {
    "kind": "plan",
    "ref": "workstreams/demo/accepted-plan.md",
    "sha256": plan_source_sha,
}
plan_assessment_projection = [
    {
        key: row[key]
        for key in ("kind", "ref", "sha256", "accepted", "reasons", "blocking_markers")
    }
    for row in plan_assessments
]
plan_acceptance_facts = {
    "selection_rule": "first_accepted_kind_plan_in_assessment_order/v1",
    "assessments": plan_assessment_projection,
    "selected_index": 1,
    "phase": "plan",
    "card": "planning-contract",
    "phase_attempt": 2,
    "declared_output_patterns": [plan_pattern],
    "target_match_count": 1,
}
plan_acceptance_facts_sha = h(
    "cortex-plan-acceptance-facts/v1", plan_acceptance_facts
)
plan_acceptance = {
    "ref": plan_snapshot_ref,
    "sha256": plan_snapshot_sha,
    "selection_rule": "first_accepted_kind_plan_in_assessment_order/v1",
    "selected_index": 1,
    "phase": "plan",
    "card": "planning-contract",
    "phase_attempt": 2,
    "output_pattern": plan_pattern,
    "match_count": 1,
}
plan_event_basis = {
    "repo": repo,
    "work_id": work_id,
    "run_id": run_id,
    "claim_key": claim_key,
    "pre_publication_authority_sha256": authority_sha,
    "phase": "plan",
    "card": "planning-contract",
    "phase_attempt": 2,
    "selected_source": plan_accepted_input,
    "acceptance_facts_sha256": plan_acceptance_facts_sha,
}
plan_event_id = h("cortex-self-publication-event/plan/v1", plan_event_basis)
plan_target = {
    "kind": "plan",
    "ref": plan_target_ref,
    "sha256": plan_source_sha,
    "output_pattern": plan_pattern,
}
plan_publication_basis = {
    "producer_event_id": plan_event_id,
    "target": plan_target,
}
plan_publication_id = h(
    "cortex-self-publication-publication/plan/v1", plan_publication_basis
)
plan_operation = {
    "path_domain": "workspace",
    "ref": plan_target_ref,
    "kind": "artifact",
    "before_exists": False,
    "before_sha256": None,
    "after_sha256": plan_source_sha,
    "mutation": True,
}
plan_intent = {
    "schema": "cortex-plan-materialization-intent/v1",
    "repo": repo,
    "work_id": work_id,
    "run_id": run_id,
    "claim_key": claim_key,
    "pre_publication_authority_sha256": authority_sha,
    "accepted_input": plan_accepted_input,
    "acceptance_evidence": plan_acceptance,
    "operation": plan_operation,
}
plan_intent_bytes = j(plan_intent)
plan_intent_sha = sha(plan_intent_bytes)
plan_intent_ref = (
    f"planning-publication-intents/run-contract-01/plan/{plan_event_id}.json"
)
plan_receipt_without_id = {
    "schema": "cortex-self-publication-receipt/v1",
    "producer_kind": "plan_materialization",
    "producer_event_id": plan_event_id,
    "publication_id": plan_publication_id,
    "repo": repo,
    "work_id": work_id,
    "run_id": run_id,
    "claim_key": claim_key,
    "pre_publication_authority_sha256": authority_sha,
    "accepted_input": plan_accepted_input,
    "acceptance_evidence": plan_acceptance,
    "published_object": plan_target,
    "transaction": {
        "kind": "cortex-plan-materialization-intent/v1",
        "intent_ref": plan_intent_ref,
        "intent_sha256": plan_intent_sha,
    },
}
plan_receipt_id = h("cortex-self-publication-receipt/v1", plan_receipt_without_id)
plan_receipt = {"receipt_id": plan_receipt_id, **plan_receipt_without_id}

# --- manager_pull_request -----------------------------------------------
candidate_sha = "0123456789abcdef0123456789abcdef01234567"
pr_metadata = {
    "title": "feat(workflow): café",
    "body": "Summary\nCloses #42",
    "labels": ["enhancement", "receipt"],
}
pr_metadata_sha = h("cortex-manager-pr-request-metadata/v1", pr_metadata)
pr_intent = {
    "schema": "cortex-manager-pr-intent/v1",
    "repo": repo,
    "work_id": work_id,
    "run_id": run_id,
    "claim_key": claim_key,
    "base": {"repository": repo, "branch": "main"},
    "head": {"repository": repo, "branch": "feature/receipt-contract", "sha": candidate_sha},
    "request_metadata": pr_metadata,
    "request_metadata_sha256": pr_metadata_sha,
}
pr_intent_bytes = j(pr_intent)
pr_intent_sha = sha(pr_intent_bytes)
pr_publication_id = h("cortex-manager-pr-intent/v1", pr_intent)
pr_marker = f"<!-- cortex-self-publication-intent:v1:{pr_publication_id} -->"
pr_rendered_body = pr_metadata["body"] + "\n" + pr_marker
pr_intent_ref = (
    f"delivery-intents/run-contract-01/manager-pull-request/{pr_publication_id}.json"
)
pr_witness = {
    "repository": repo,
    "number": 7,
    "id": 7007,
    "node_id": "PR_kwDOExample0001",
}
pr_published = {
    "repository": repo,
    "number": pr_witness["number"],
    "id": pr_witness["id"],
    "node_id": pr_witness["node_id"],
    "state": "open",
    "head": {
        "repository": repo,
        "branch": "feature/receipt-contract",
        "sha": candidate_sha,
    },
    "base": {"repository": repo, "branch": "main"},
    "marker": pr_marker,
    "request_metadata_sha256": pr_metadata_sha,
}
pr_event_basis = {
    "repo": repo,
    "work_id": work_id,
    "run_id": run_id,
    "claim_key": claim_key,
    "ship_step": {"phase": "ship", "card": "open-pr"},
    "ship_attempt": 1,
    "candidate": {
        "repository": repo,
        "branch": "feature/receipt-contract",
        "sha": candidate_sha,
    },
}
pr_event_id = h("cortex-self-publication-event/manager-pr/v1", pr_event_basis)
pr_acceptance = {
    "intent_ref": pr_intent_ref,
    "intent_sha256": pr_intent_sha,
    "post_witness": pr_witness,
}
pr_receipt_without_id = {
    "schema": "cortex-self-publication-receipt/v1",
    "producer_kind": "manager_pull_request",
    "producer_event_id": pr_event_id,
    "publication_id": pr_publication_id,
    "repo": repo,
    "work_id": work_id,
    "run_id": run_id,
    "claim_key": claim_key,
    "pre_publication_authority_sha256": authority_sha,
    "accepted_input": {
        "repository": repo,
        "branch": "feature/receipt-contract",
        "candidate_sha": candidate_sha,
    },
    "acceptance_evidence": pr_acceptance,
    "published_object": pr_published,
    "transaction": {
        "kind": "cortex-manager-pr-intent/v1",
        "intent_ref": pr_intent_ref,
        "intent_sha256": pr_intent_sha,
    },
}
pr_receipt_id = h("cortex-self-publication-receipt/v1", pr_receipt_without_id)
pr_receipt = {"receipt_id": pr_receipt_id, **pr_receipt_without_id}


def producer_vector(
    *,
    snapshot: dict[str, object] | None,
    snapshot_ref: str | None,
    snapshot_bytes: bytes | None,
    evidence_payload: dict[str, object] | None,
    evidence_bytes: bytes | None,
    intent: dict[str, object],
    intent_bytes: bytes,
    intent_ref: str,
    event_basis: dict[str, object],
    event_domain: str,
    event_id: str,
    publication_basis: dict[str, object] | None,
    publication_domain: str,
    publication_id: str,
    receipt_without_id: dict[str, object],
    receipt_id: str,
    receipt: dict[str, object],
    extras: dict[str, object],
) -> dict[str, object]:
    result: dict[str, object] = {
        "event": structured(event_domain, event_basis),
        "intent_core": {
            **obj(intent),
            "stored_bytes_utf8": intent_bytes.decode("utf-8"),
            "sha256": sha(intent_bytes),
            "ref": intent_ref,
        },
        "publication_id": {
            "domain": publication_domain,
            "basis": obj(publication_basis) if publication_basis is not None else None,
            "value": publication_id,
        },
        "receipt_id": structured(
            "cortex-self-publication-receipt/v1", receipt_without_id
        ),
        "receipt": receipt,
    }
    if snapshot is not None and snapshot_bytes is not None:
        result["accepted_input_snapshot"] = {
            **obj(snapshot),
            "stored_bytes_utf8": snapshot_bytes.decode("utf-8"),
            "sha256": sha(snapshot_bytes),
            "ref": snapshot_ref,
        }
    if evidence_payload is not None and evidence_bytes is not None:
        result["peer_evidence"] = {
            "payload": obj(evidence_payload),
            "stored_bytes_utf8": evidence_bytes.decode("utf-8"),
            "sha256": sha(evidence_bytes),
        }
    result.update(extras)
    return result


vectors = {
    "generator": {
        "implementation": "Python stdlib hashlib + json only",
        "production_imports": [],
        "canonical_json": "json.dumps(ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')",
        "hash": "sha256(ASCII(domain + LF) || canonical_json(value))",
    },
    "shared_fixture_identity": {
        "repo": repo,
        "work_id": work_id,
        "run_id": run_id,
        "pre_publication_authority_sha256": authority_sha,
        "claim_key_basis": obj(
            {"repo": repo, "work_id": work_id, "authority_digest": authority_sha}
        ),
        "claim_key": claim_key,
        "note": "Synthetic shape fixture; claim key is independently derived using the repo's documented claim:v1 basis, but this is not a WorkAuthority provenance witness.",
    },
    "generic_vectors": [
        structured("test/v1", {}),
        structured("test/v1", {"a": 1}),
        structured("test/v1", {"z": [True, None, 3.5], "é": "咖啡", "a": {"b": False}}),
    ],
    "producer_vectors": {
        "brainstorm_artifact": producer_vector(
            snapshot=brain_snapshot,
            snapshot_ref=brain_snapshot_ref,
            snapshot_bytes=brain_snapshot_bytes,
            evidence_payload=brain_evidence_payload,
            evidence_bytes=brain_evidence_bytes,
            intent=brain_intent,
            intent_bytes=brain_intent_bytes,
            intent_ref=brain_intent_ref,
            event_basis=brain_event_basis,
            event_domain="cortex-self-publication-event/brainstorm/v1",
            event_id=brain_event_id,
            publication_basis=brain_publication_basis,
            publication_domain="cortex-self-publication-publication/brainstorm/v1",
            publication_id=brain_publication_id,
            receipt_without_id=brain_receipt_without_id,
            receipt_id=brain_receipt_id,
            receipt=brain_receipt,
            extras={
                "artifact_bytes": {
                    "original_plan": raw_bytes(brain_plan_text.encode("utf-8")),
                    **{
                        f"published_{row['kind']}": raw_bytes(
                            row["content"].encode("utf-8")
                        )
                        for row in brain_outputs
                    },
                },
                "operations": brain_operations,
                "snapshot_path": brain_snapshot_ref,
                "task_slug": "contract-1029",
                "declared_output_patterns": [row["output_pattern"] for row in brain_outputs],
                "question_pack_derivation": {
                    "production_algorithm": "planning._build_default_question_pack + _make_question",
                    "planning_kinds": ["spec", "design", "plan"],
                    "accepted_refs_in_assessment_order": [brain_plan_ref],
                    "missing_kinds_in_production_order": ["spec", "design"],
                    "questions": [
                        {
                            "identity": obj(
                                {
                                    "kind": row["kind"],
                                    "prompt": row["prompt"],
                                    "source_refs": row["source_refs"],
                                }
                            ),
                            "question": row,
                            "question_id_digest_prefix16": row["question_id"][2:],
                        }
                        for row in brain_questions
                    ],
                    "pack_body": obj(brain_questions),
                    "pack_id": question_pack["pack_id"],
                    "accepted_question_pack_equals_default": True,
                },
                "multi_output_receipts": brain_receipts,
                "publication_bases": brain_publication_bases,
            },
        ),
        "plan_materialization": producer_vector(
            snapshot=plan_snapshot,
            snapshot_ref=plan_snapshot_ref,
            snapshot_bytes=plan_snapshot_bytes,
            evidence_payload=None,
            evidence_bytes=None,
            intent=plan_intent,
            intent_bytes=plan_intent_bytes,
            intent_ref=plan_intent_ref,
            event_basis=plan_event_basis,
            event_domain="cortex-self-publication-event/plan/v1",
            event_id=plan_event_id,
            publication_basis=plan_publication_basis,
            publication_domain="cortex-self-publication-publication/plan/v1",
            publication_id=plan_publication_id,
            receipt_without_id=plan_receipt_without_id,
            receipt_id=plan_receipt_id,
            receipt=plan_receipt,
            extras={
                "acceptance_facts": structured(
                    "cortex-plan-acceptance-facts/v1", plan_acceptance_facts
                ),
                "source_bytes": {
                    "rejected_candidate": raw_bytes(plan_rejected_text.encode("utf-8")),
                    "selected_source": raw_bytes(plan_accepted_text.encode("utf-8")),
                },
                "target": plan_target,
                "snapshot_path": plan_snapshot_ref,
            },
        ),
        "manager_pull_request": producer_vector(
            snapshot=None,
            snapshot_ref=None,
            snapshot_bytes=None,
            evidence_payload=None,
            evidence_bytes=None,
            intent=pr_intent,
            intent_bytes=pr_intent_bytes,
            intent_ref=pr_intent_ref,
            event_basis=pr_event_basis,
            event_domain="cortex-self-publication-event/manager-pr/v1",
            event_id=pr_event_id,
            publication_basis=pr_intent,
            publication_domain="cortex-manager-pr-intent/v1",
            publication_id=pr_publication_id,
            receipt_without_id=pr_receipt_without_id,
            receipt_id=pr_receipt_id,
            receipt=pr_receipt,
            extras={
                "request_metadata": {
                    **obj(pr_metadata),
                    "sha256": pr_metadata_sha,
                },
                "marker": pr_marker,
                "rendered_body": pr_rendered_body,
                "post_witness": pr_witness,
                "read_back_observation": pr_published,
                "candidate_sha": candidate_sha,
            },
        ),
    },
    "raw_rejection_inputs": [
        {
            "raw": '{"schema":"v1","schema":"v2"}',
            "reason": "duplicate object key",
            "expected": "reject strict value; if inside publication_receipts[i], preserve exact row source in invalid-json-row wrapper",
        },
        {"raw": '{"value":NaN}', "reason": "non-finite JSON number", "expected": "reject"},
        {"raw": '{"value":Infinity}', "reason": "non-finite JSON number", "expected": "reject"},
        {"raw": '{"value":-Infinity}', "reason": "non-finite JSON number", "expected": "reject"},
        {"raw": '{"value":"\\ud800"}', "reason": "unpaired Unicode surrogate", "expected": "reject"},
    ],
}


def brainstorm_join_vector(
    vector_id: str,
    output_kind: str,
    operation: dict[str, object],
    evidence_rows: list[dict[str, object]],
    *,
    peer_scope: dict[str, object] | None = None,
    peer_evidence_ref: str = brain_evidence_abs_ref,
    peer_evidence_sha256: str = brain_evidence_sha,
    producer_event_id: str = brain_event_id,
    transaction_operations: list[dict[str, object]] | None = None,
    integration_artifacts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    scope = peer_scope if peer_scope is not None else brain_evidence_payload["scope"]
    run_scope = {
        "repo": repo,
        "work_id": work_id,
        "planning_source_revision": brain_source_revision,
    }
    scope_matches = (
        set(scope) == {"repo", "work_id", "source_revision"}
        and scope["repo"] == run_scope["repo"]
        and scope["work_id"] == run_scope["work_id"]
        and scope["source_revision"] == run_scope["planning_source_revision"]
    )
    before_state_valid = (
        (operation["before_exists"] is False and operation["before_sha256"] is None)
        or (
            operation["before_exists"] is True
            and isinstance(operation["before_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", operation["before_sha256"]) is not None
        )
    )
    tx_operations = (
        transaction_operations
        if transaction_operations is not None
        else brain_operations
    )
    integration_rows_source = (
        integration_artifacts
        if integration_artifacts is not None
        else brain_evidence_payload["primary_integration"]["artifacts"]
    )
    output_rows = [
        row
        for row in evidence_rows
        if row.get("kind") == output_kind
        and row.get("ref") == operation["ref"]
        and row.get("sha256") == operation["after_sha256"]
    ]
    integration_rows = [
        row
        for row in integration_rows_source
        if row.get("kind") == output_kind
        and row.get("path") == operation["ref"]
    ]
    evidence_write_operations = [
        row
        for row in tx_operations
        if row.get("path_domain") == "coordinator"
        and row.get("kind") == "evidence"
        and row.get("ref") == brain_evidence_operation["ref"]
        and row.get("after_sha256") == peer_evidence_sha256
    ]
    selected_operation_in_journal = operation in tx_operations
    transaction_join_valid = (
        peer_evidence_ref == brain_evidence_abs_ref
        and len(evidence_write_operations) == 1
        and selected_operation_in_journal
    )
    eligibility_ok = (
        operation["path_domain"] == "workspace"
        and operation["kind"] == "artifact"
        and before_state_valid
        and operation["mutation"] is True
        and scope_matches
        and transaction_join_valid
        and len(output_rows) == 1
        and len(integration_rows) == 1
    )
    if not scope_matches:
        reason = "peer-evidence-scope-mismatch"
    elif operation["mutation"] is not True:
        reason = "operation-did-not-mutate"
    elif not transaction_join_valid:
        reason = "peer-evidence-transaction-join-mismatch"
    elif eligibility_ok:
        reason = None
    else:
        reason = "operation-evidence-join-ineligible"
    return {
        "id": vector_id,
        "producer_event_id": producer_event_id,
        "peer_evidence_ref": peer_evidence_ref,
        "peer_evidence_sha256": peer_evidence_sha256,
        "peer_evidence_scope": scope,
        "captured_run_scope": run_scope,
        "scope_matches_captured_run": scope_matches,
        "peer_evidence_gate_ref": {
            "kind": "brainstorm",
            "ref": peer_evidence_ref,
            "sha256": peer_evidence_sha256,
        },
        "transaction_join": {
            "matching_evidence_write_operations": evidence_write_operations,
            "selected_output_operation_in_journal": selected_operation_in_journal,
        },
        "operation": operation,
        "matching_peer_evidence_rows": output_rows,
        "matching_integration_rows": integration_rows,
        "all_peer_evidence_rows": evidence_rows,
        "expected": {
            "mint_receipt": eligibility_ok,
            "reason": reason,
        },
    }


brainstorm_operation_vectors = [
    brainstorm_join_vector(
        f"brainstorm-{output['kind']}-unique-evidence-operation-join",
        output["kind"],
        operation,
        brain_evidence_payload["artifacts"],
    )
    for output, operation in zip(
        brain_outputs, brain_artifact_operations, strict=True
    )
]
unchanged_brain_operation = {
    **brain_artifact_operations[0],
    "before_exists": True,
    "before_sha256": brain_artifact_operations[0]["after_sha256"],
    "mutation": False,
}
brainstorm_operation_vectors.append(
    brainstorm_join_vector(
        "brainstorm-unchanged-no-write-same-byte-does-not-mint",
        brain_outputs[0]["kind"],
        unchanged_brain_operation,
        brain_evidence_payload["artifacts"],
    )
)
overwrite_brain_operation = {
    **brain_artifact_operations[0],
    "before_exists": True,
    "before_sha256": sha(b"previous authority-owned brainstorm artifact bytes\n"),
    "mutation": True,
}
overwrite_transaction_operations = [
    overwrite_brain_operation if row == brain_artifact_operations[0] else row
    for row in brain_operations
]
brainstorm_operation_vectors.append(
    brainstorm_join_vector(
        "brainstorm-overwrite-existing-authority-owned-artifact-mints",
        brain_outputs[0]["kind"],
        overwrite_brain_operation,
        brain_evidence_payload["artifacts"],
        transaction_operations=overwrite_transaction_operations,
    )
)
duplicate_brain_evidence = [
    *brain_evidence_payload["artifacts"],
    next(
        row
        for row in brain_evidence_payload["artifacts"]
        if row["kind"] == brain_outputs[0]["kind"]
        and row["ref"] == brain_outputs[0]["ref"]
        and row["sha256"] == brain_outputs[0]["sha256"]
    ),
]
brainstorm_operation_vectors.append(
    brainstorm_join_vector(
        "brainstorm-duplicate-peer-evidence-row-does-not-mint",
        brain_outputs[0]["kind"],
        brain_artifact_operations[0],
        duplicate_brain_evidence,
    )
)
changed_peer_evidence = [
    {
        **row,
        "sha256": "f" * 64,
    }
    if row["kind"] == brain_outputs[0]["kind"]
    and row["ref"] == brain_outputs[0]["ref"]
    else row
    for row in brain_evidence_payload["artifacts"]
]
brainstorm_operation_vectors.append(
    brainstorm_join_vector(
        "brainstorm-peer-evidence-hash-mismatch-does-not-mint",
        brain_outputs[0]["kind"],
        brain_artifact_operations[0],
        changed_peer_evidence,
    )
)
missing_peer_evidence = [
    row
    for row in brain_evidence_payload["artifacts"]
    if row["kind"] != brain_outputs[0]["kind"]
    or row["ref"] != brain_outputs[0]["ref"]
    or row["sha256"] != brain_outputs[0]["sha256"]
]
brainstorm_operation_vectors.append(
    brainstorm_join_vector(
        "brainstorm-peer-evidence-row-missing-does-not-mint",
        brain_outputs[0]["kind"],
        brain_artifact_operations[0],
        missing_peer_evidence,
    )
)
wrong_scope_payload = json.loads(json.dumps(brain_evidence_payload))
wrong_scope_payload["scope"]["source_revision"] = "f" * 40
wrong_scope_evidence_bytes = j(wrong_scope_payload) + b"\n"
wrong_scope_evidence_sha = sha(wrong_scope_evidence_bytes)
wrong_scope_event_basis = json.loads(json.dumps(brain_event_basis))
wrong_scope_event_basis["brainstorm_evidence"]["sha256"] = wrong_scope_evidence_sha
wrong_scope_event_id = h(
    "cortex-self-publication-event/brainstorm/v1", wrong_scope_event_basis
)
wrong_scope_transaction_operations = json.loads(json.dumps(brain_operations))
for row in wrong_scope_transaction_operations:
    if row["kind"] == "evidence":
        row["after_sha256"] = wrong_scope_evidence_sha
wrong_scope_transaction_operations.sort(
    key=lambda row: (row["path_domain"], row["ref"], row["kind"], row["after_sha256"])
)
wrong_scope_vector = brainstorm_join_vector(
    "brainstorm-peer-evidence-scope-source-revision-mismatch-does-not-mint",
    brain_outputs[0]["kind"],
    brain_artifact_operations[0],
    wrong_scope_payload["artifacts"],
    peer_scope=wrong_scope_payload["scope"],
    peer_evidence_sha256=wrong_scope_evidence_sha,
    producer_event_id=wrong_scope_event_id,
    transaction_operations=wrong_scope_transaction_operations,
    integration_artifacts=wrong_scope_payload["primary_integration"]["artifacts"],
)
wrong_scope_vector.update(
    {
        "boundary": "brainstorm-peer-evidence-scope-and-transaction-join",
        "peer_evidence_payload": wrong_scope_payload,
        "peer_evidence_stored_bytes_utf8": wrong_scope_evidence_bytes.decode("utf-8"),
        "peer_evidence_sha256_recomputed": wrong_scope_evidence_sha,
        "event_basis": wrong_scope_event_basis,
        "transaction_operations": wrong_scope_transaction_operations,
        "expected": {
            "mint_receipt": False,
            "reason": "peer-evidence-scope-mismatch",
        },
    }
)
brainstorm_operation_vectors.append(wrong_scope_vector)
wrong_domain_operation = {
    **brain_artifact_operations[0],
    "path_domain": "coordinator",
}
brainstorm_operation_vectors.append(
    brainstorm_join_vector(
        "brainstorm-coordinator-domain-artifact-does-not-mint",
        brain_outputs[0]["kind"],
        wrong_domain_operation,
        brain_evidence_payload["artifacts"],
    )
)
plan_unchanged_operation = {
    **plan_operation,
    "before_exists": True,
    "before_sha256": plan_source_sha,
    "mutation": False,
}
vectors["producer_operation_eligibility_vectors"] = [
    *brainstorm_operation_vectors,
    {
        "id": "plan-new-workspace-target-mints-eligible-operation",
        "producer_event_id": plan_event_id,
        "target": plan_target,
        "operation": plan_operation,
        "expected": {"mint_receipt": True},
    },
    {
        "id": "plan-existing-same-byte-target-does-not-mint",
        "producer_event_id": plan_event_id,
        "target": plan_target,
        "existing_target_sha256": plan_source_sha,
        "operation": plan_unchanged_operation,
        "expected": {
            "mint_receipt": False,
            "reason": "target-existed-and-was-not-mutated",
        },
    },
]


# Negative fixtures are full literal values derived from the positive fixtures.
# Re-seal mutations when the intended failure is a deeper cross-binding check.
def reseal(receipt: dict[str, object]) -> dict[str, object]:
    body = {key: value for key, value in receipt.items() if key != "receipt_id"}
    return {"receipt_id": h("cortex-self-publication-receipt/v1", body), **body}


def clone(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


negative_vectors = []


def add_negative(
    vector_id: str,
    value: object,
    expected_code: str,
    *,
    boundary: str = "strict-receipt-value-parser",
    context: object | None = None,
) -> None:
    negative_vectors.append(
        {
            "id": vector_id,
            "boundary": boundary,
            "input": value,
            "context": context,
            "expected": {
                "accepted": False,
                "diagnostic_code": expected_code,
                "persist": False,
            },
        }
    )


base_brain = clone(brain_receipts[0])
base_plan = clone(plan_receipt)
base_pr = clone(pr_receipt)

mutated = clone(base_brain)
mutated["schema"] = "cortex-self-publication-receipt/v2"
add_negative("unknown-schema", mutated, "unknown-schema")

mutated = clone(base_brain)
mutated["producer_kind"] = "unknown_producer"
add_negative("unknown-producer-kind", mutated, "unknown-producer-kind")

mutated = clone(base_plan)
del mutated["transaction"]["intent_sha256"]
add_negative("missing-nested-key", mutated, "malformed-known-v1")

mutated = clone(base_plan)
mutated["unexpected"] = "must be rejected"
add_negative("extra-envelope-key", mutated, "malformed-known-v1")

mutated = clone(base_plan)
mutated["accepted_input"] = []
add_negative("wrong-union-type", mutated, "malformed-known-v1")

mutated = clone(base_brain)
mutated["pre_publication_authority_sha256"] = "A" + "a" * 63
add_negative("uppercase-digest", mutated, "invalid-digest")

mutated = clone(base_brain)
mutated["accepted_input"]["sha256"] = "0" * 63
add_negative("short-digest", mutated, "invalid-digest")

mutated = clone(base_pr)
mutated["transaction"]["kind"] = "cortex-plan-materialization-intent/v1"
mutated = reseal(mutated)
add_negative(
    "cross-variant-transaction-kind",
    mutated,
    "cross-binding",
    context={"referenced_intent_schema": "cortex-manager-pr-intent/v1"},
)

mutated = clone(base_plan)
mutated["producer_event_id"] = "0" * 64
mutated = reseal(mutated)
add_negative(
    "event-id-does-not-match-basis",
    mutated,
    "cross-binding",
    boundary="sidecar-resolver-validation",
    context={"expected_event_basis": plan_event_basis},
)

mutated = clone(base_plan)
mutated["publication_id"] = "0" * 64
mutated = reseal(mutated)
add_negative(
    "publication-id-does-not-match-basis",
    mutated,
    "cross-binding",
    boundary="sidecar-resolver-validation",
    context={"expected_publication_basis": plan_publication_basis},
)

mutated = clone(base_plan)
mutated["published_object"]["ref"] = "docs/superpowers/plans/other.md"
mutated = reseal(mutated)
add_negative(
    "published-object-does-not-match-intent-operation",
    mutated,
    "cross-binding",
    boundary="sidecar-resolver-validation",
    context={"referenced_intent_core": plan_intent},
)

mutated = clone(base_plan)
mutated["receipt_id"] = "0" * 64
add_negative("receipt-id-contained-formula-mismatch", mutated, "invalid-digest")

mutated = clone(base_pr)
mutated["acceptance_evidence"]["post_witness"]["number"] = True
mutated["published_object"]["number"] = True
mutated = reseal(mutated)
add_negative("boolean-is-not-positive-integer", mutated, "malformed-known-v1")

negative_vectors.extend(
    [
        {
            "id": "duplicate-json-key-in-workflow-receipt-row",
            "boundary": "isolated-raw-registry-row-decoder",
            "registry_json_pointer": "/workflows/0/publication_receipts/0",
            "input_raw_row": '{"schema": "v1", "schema": "v2"}',
            "scope_note": "Isolated raw receipt-row decoder fixture invoked at the real registry field pointer. It is not a complete registry document or loadable WorkflowRun; history_loads is false and this vector claims decoder classification only.",
            "expected_invalid_row": {
                "kind": "cortex-publication-receipt-invalid-json-row/v1",
                "raw_json": '{"schema": "v1", "schema": "v2"}',
                "diagnostic": {"code": "duplicate-json-key", "index": 0},
            },
            "expected": {
                "row_decoder": "classified-invalid-json-row",
                "history_loads": False,
                "append": "not-run",
                "persist": False,
            },
        },
        {
            "id": "invalid-non-list-container-round-trip",
            "boundary": "workflow-run-value-round-trip",
            "input_field": {"legacy": "opaque"},
            "expected_serialized_field": {
                "kind": "cortex-publication-receipts-invalid-container/v1",
                "raw": {"legacy": "opaque"},
                "diagnostic": {"code": "container-not-list"},
            },
            "expected_after_from_dict_to_dict": {
                "kind": "cortex-publication-receipts-invalid-container/v1",
                "raw": {"legacy": "opaque"},
                "diagnostic": {"code": "container-not-list"},
            },
            "expected": {"append": "conflict", "persist": False},
        },
        {
            "id": "invalid-row-wrapper-round-trip",
            "boundary": "workflow-run-value-round-trip",
            "input_field": [{"schema": "future-receipt/v9", "value": 3}],
            "expected_serialized_field": [
                {
                    "kind": "cortex-publication-receipt-invalid-row/v1",
                    "raw": {"schema": "future-receipt/v9", "value": 3},
                    "diagnostic": {"code": "unknown-schema", "index": 0},
                }
            ],
            "expected_after_from_dict_to_dict": [
                {
                    "kind": "cortex-publication-receipt-invalid-row/v1",
                    "raw": {"schema": "future-receipt/v9", "value": 3},
                    "diagnostic": {"code": "unknown-schema", "index": 0},
                }
            ],
            "expected": {"append": "conflict", "persist": False},
        },
        {
            "id": "unknown-wrapper-round-trip",
            "boundary": "workflow-run-value-round-trip",
            "input_field": {
                "kind": "future-publication-receipts-wrapper/v9",
                "raw": ["opaque"],
            },
            "expected_after_from_dict_to_dict": {
                "kind": "future-publication-receipts-wrapper/v9",
                "raw": ["opaque"],
            },
            "expected": {"append": "conflict", "persist": False, "never_coerce_to_list": True},
        },
    ]
)
vectors["negative_vectors"] = negative_vectors


# Valid alternate PR receipts exercise replay conflicts beyond malformed-value
# rejection. The fixture changes only fields that the PR variant binds together.
def pr_receipt_with_witness(
    base: dict[str, object], number: int, rest_id: int, node_id: str
) -> dict[str, object]:
    receipt = clone(base)
    witness = receipt["acceptance_evidence"]["post_witness"]
    observed = receipt["published_object"]
    tuple_value = {
        "repository": repo,
        "number": number,
        "id": rest_id,
        "node_id": node_id,
    }
    witness.update(tuple_value)
    observed.update(tuple_value)
    return reseal(receipt)


def pr_receipt_with_new_metadata(
    base: dict[str, object], title: str
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    intent = clone(pr_intent)
    intent["request_metadata"]["title"] = title
    metadata_digest = h(
        "cortex-manager-pr-request-metadata/v1", intent["request_metadata"]
    )
    intent["request_metadata_sha256"] = metadata_digest
    intent_bytes = j(intent)
    intent_sha256 = sha(intent_bytes)
    publication_id = h("cortex-manager-pr-intent/v1", intent)
    marker = f"<!-- cortex-self-publication-intent:v1:{publication_id} -->"
    intent_ref = (
        f"delivery-intents/run-contract-01/manager-pull-request/{publication_id}.json"
    )
    receipt = clone(base)
    receipt["publication_id"] = publication_id
    receipt["acceptance_evidence"]["intent_ref"] = intent_ref
    receipt["acceptance_evidence"]["intent_sha256"] = intent_sha256
    receipt["transaction"]["intent_ref"] = intent_ref
    receipt["transaction"]["intent_sha256"] = intent_sha256
    receipt["published_object"]["marker"] = marker
    receipt["published_object"]["request_metadata_sha256"] = metadata_digest
    return reseal(receipt), intent, {
        "intent_ref": intent_ref,
        "intent_sha256": intent_sha256,
        "request_metadata_sha256": metadata_digest,
        "marker": marker,
    }


mutated = clone(base_pr)
mutated["published_object"]["number"] += 1
mutated = reseal(mutated)
add_negative(
    "post-witness-get-number-mismatch",
    mutated,
    "cross-binding",
    context={"post_witness": pr_witness, "expected_observation": pr_published},
)

for field, wrong_value in (
    ("repository", "example-org/other"),
    ("id", pr_witness["id"] + 1),
    ("node_id", "PR_kwDOExampleMismatch"),
):
    mutated = clone(base_pr)
    mutated["published_object"][field] = wrong_value
    mutated = reseal(mutated)
    add_negative(
        f"post-witness-get-{field}-mismatch",
        mutated,
        "cross-binding",
        context={"post_witness": pr_witness, "expected_observation": pr_published},
    )

mutated = clone(base_pr)
mutated["published_object"]["marker"] = (
    "<!-- cortex-self-publication-intent:v1:" + "0" * 64 + " -->"
)
mutated = reseal(mutated)
add_negative(
    "read-back-marker-does-not-bind-publication",
    mutated,
    "cross-binding",
    context={"expected_marker": pr_marker, "intent_core": pr_intent},
)

for marker_case, wrong_marker in (
    ("duplicate", pr_marker + "\n" + pr_marker),
    ("leading-whitespace", " " + pr_marker),
    ("trailing-whitespace", pr_marker + " "),
    ("wrong-case", pr_marker.replace("cortex-self-publication-intent", "CORTEX-SELF-PUBLICATION-INTENT")),
):
    mutated = clone(base_pr)
    mutated["published_object"]["marker"] = wrong_marker
    mutated = reseal(mutated)
    add_negative(
        f"read-back-marker-{marker_case}-rejected",
        mutated,
        "cross-binding",
        context={"expected_marker": pr_marker, "intent_core": pr_intent},
    )

mutated = clone(base_pr)
mutated["published_object"]["request_metadata_sha256"] = "0" * 64
mutated = reseal(mutated)
add_negative(
    "read-back-metadata-digest-mismatch",
    mutated,
    "cross-binding",
    boundary="sidecar-resolver-validation",
    context={"expected_request_metadata_sha256": pr_metadata_sha},
)

mutated = clone(base_pr)
mutated["accepted_input"]["candidate_sha"] = "f" * 40
mutated = reseal(mutated)
add_negative(
    "pr-accepted-input-candidate-does-not-match-intent",
    mutated,
    "cross-binding",
    boundary="sidecar-resolver-validation",
    context={"intent_head": pr_intent["head"]},
)
vectors["negative_vectors"] = negative_vectors

raw_metadata = {
    "title": "cafe\u0301: feat",
    "body": "Summary\r\nCloses #42\rDetails café\rEnd",
    "labels": ["receipt", "cafe\u0301", "enhancement"],
}
normalized_metadata = {
    "title": unicodedata.normalize("NFC", raw_metadata["title"]),
    "body": raw_metadata["body"].replace("\r\n", "\n").replace("\r", "\n"),
    "labels": sorted(
        {
            unicodedata.normalize("NFC", label)
            for label in raw_metadata["labels"]
        }
    ),
}
normalized_metadata_sha = h(
    "cortex-manager-pr-request-metadata/v1", normalized_metadata
)
normalized_intent = clone(pr_intent)
normalized_intent["request_metadata"] = normalized_metadata
normalized_intent["request_metadata_sha256"] = normalized_metadata_sha
normalized_publication_id = h("cortex-manager-pr-intent/v1", normalized_intent)
normalized_marker = (
    f"<!-- cortex-self-publication-intent:v1:{normalized_publication_id} -->"
)
vectors["pr_request_normalization_vectors"] = [
    {
        "id": "nfc-and-line-ending-normalization",
        "boundary": "resolved-intent-sidecar-pure-value-validation",
        "intent_value_source": "parsed value from referenced immutable PR intent sidecar",
        "raw_request_metadata": raw_metadata,
        "normalized_request_metadata": normalized_metadata,
        "request_metadata_digest": structured(
            "cortex-manager-pr-request-metadata/v1", normalized_metadata
        ),
        "intent_core": obj(normalized_intent),
        "publication_id": structured(
            "cortex-manager-pr-intent/v1", normalized_intent
        ),
        "marker": normalized_marker,
        "rendered_body": normalized_metadata["body"] + "\n" + normalized_marker,
        "expected": {"accepted": True},
    },
    {
        "id": "marker-line-in-input-body-is-rejected",
        "raw_request_metadata": {
            "title": "feat: marker check",
            "body": "Summary\n<!-- cortex-self-publication-intent:v1:" + "a" * 64 + " -->",
            "labels": ["enhancement"],
        },
        "expected": {"accepted": False, "diagnostic_code": "marker-like-line"},
    },
    {
        "id": "marker-publication-id-mismatch-in-readback-body-is-rejected",
        "boundary": "github-get-raw-response-body",
        "raw_get_response_body_utf8": "Summary\n<!-- cortex-self-publication-intent:v1:" + "b" * 64 + " -->",
        "expected": {"accepted": False, "diagnostic_code": "malformed-marker-like-line"},
    },
    {
        "id": "duplicate-labels-after-nfc-are-deduplicated",
        "raw_request_metadata": {
            "title": "feat: duplicate labels",
            "body": "Summary",
            "labels": ["receipt", "cafe\u0301", "café", "enhancement"],
        },
        "expected_normalized_labels": ["café", "enhancement", "receipt"],
        "expected": {"accepted": True},
    },
]


def get_body_vector(vector_id: str, raw_body: str) -> dict[str, object]:
    marker_like_lines = [
        line
        for line in re.split(r"\r\n|\r|\n", raw_body)
        if "cortex-self-publication-intent:" in line.casefold()
    ]
    exact_marker_lines = [line for line in marker_like_lines if line == pr_marker]
    normalized_get_body = raw_body.replace("\r\n", "\n").replace("\r", "\n")
    body_matches_rendered = normalized_get_body == pr_rendered_body
    accepted = (
        len(marker_like_lines) == 1
        and len(exact_marker_lines) == 1
        and body_matches_rendered
    )
    if accepted:
        reason = None
    elif len(marker_like_lines) != 1:
        reason = "marker-like-line-count"
    elif len(exact_marker_lines) != 1:
        reason = "malformed-marker-like-line"
    else:
        reason = "normalized-body-not-equal-rendered-body"
    return {
        "id": vector_id,
        "boundary": "github-get-raw-response-body",
        "get_response_raw_body_utf8": raw_body,
        "line_ending_normalized_get_body": normalized_get_body,
        "expected_rendered_marker_bearing_body": pr_rendered_body,
        "raw_marker_like_lines": marker_like_lines,
        "exact_expected_marker_line_count": len(exact_marker_lines),
        "normalized_get_body_equals_rendered_body": body_matches_rendered,
        "expected": {"accepted": accepted, "reason": reason},
    }


vectors["pr_get_raw_body_vectors"] = [
    get_body_vector("exact-rendered-body-accepted", pr_rendered_body),
    get_body_vector(
        "crlf-rendered-body-accepted-after-line-ending-normalization",
        pr_rendered_body.replace("\n", "\r\n"),
    ),
    get_body_vector(
        "cr-rendered-body-accepted-after-line-ending-normalization",
        pr_rendered_body.replace("\n", "\r"),
    ),
    get_body_vector(
        "duplicate-exact-marker-lines-rejected", pr_rendered_body + "\n" + pr_marker
    ),
    get_body_vector(
        "leading-whitespace-marker-line-rejected",
        pr_metadata["body"] + "\n " + pr_marker,
    ),
    get_body_vector(
        "trailing-whitespace-marker-line-rejected",
        pr_metadata["body"] + "\n" + pr_marker + " ",
    ),
    get_body_vector(
        "case-altered-marker-line-rejected",
        pr_metadata["body"]
        + "\n"
        + pr_marker.replace(
            "cortex-self-publication-intent", "CORTEX-SELF-PUBLICATION-INTENT"
        ),
    ),
    get_body_vector(
        "wrong-publication-id-marker-line-rejected",
        pr_metadata["body"]
        + "\n"
        + pr_marker.replace(pr_publication_id, "b" * 64),
    ),
    get_body_vector(
        "malformed-version-marker-line-rejected",
        pr_metadata["body"] + "\n" + pr_marker.replace(":v1:", ":v2:"),
    ),
    get_body_vector(
        "marker-present-but-body-differs-from-rendered-body-rejected",
        "Changed summary\n" + pr_marker,
    ),
]

vectors["receipt_history_round_trip_vectors"] = [
    {
        "id": "legacy-field-absent-materializes-empty-on-write",
        "input_publication_receipts_member": "absent",
        "expected_after_from_dict": [],
        "expected_after_to_dict_and_reload": [],
    },
    {
        "id": "explicit-empty-history-round-trip",
        "input_publication_receipts_member": [],
        "expected_after_from_dict": [],
        "expected_after_to_dict_and_reload": [],
    },
    {
        "id": "valid-row-round-trip",
        "input_publication_receipts_member": [pr_receipt],
        "expected_after_from_dict": [pr_receipt],
        "expected_after_to_dict_and_reload": [pr_receipt],
        "sidecar_resolver_calls_during_from_dict": 0,
        "expected_reload_state": "typed-valid-value; provenance-unverified",
    },
    {
        "id": "typed-valid-reload-without-sidecars",
        "input_publication_receipts_member": [plan_receipt],
        "sidecars_available_to_from_dict": False,
        "sidecar_resolver_calls_during_from_dict": 0,
        "expected_reload_state": "typed-valid-value; provenance-unverified",
        "expected_after_to_dict_and_reload": [plan_receipt],
    },
    {
        "id": "invalid-json-row-wrapper-value-round-trip-after-isolated-decoding",
        "boundary": "workflow-run-value-round-trip-after-decoder-classification",
        "input_publication_receipts_member": [
            {
                "kind": "cortex-publication-receipt-invalid-json-row/v1",
                "raw_json": '{"schema": "v1", "schema": "v2"}',
                "diagnostic": {"code": "duplicate-json-key", "index": 0},
            }
        ],
        "expected_after_from_dict": [
            {
                "kind": "cortex-publication-receipt-invalid-json-row/v1",
                "raw_json": '{"schema": "v1", "schema": "v2"}',
                "diagnostic": {"code": "duplicate-json-key", "index": 0},
            }
        ],
        "expected_after_to_dict_and_reload": [
            {
                "kind": "cortex-publication-receipt-invalid-json-row/v1",
                "raw_json": '{"schema": "v1", "schema": "v2"}',
                "diagnostic": {"code": "duplicate-json-key", "index": 0},
            }
        ],
        "full_registry_load_claimed": False,
    },
    {
        "id": "opaque-non-list-container-round-trip",
        "input_publication_receipts_member": {"legacy": "opaque"},
        "expected_after_from_dict": {
            "kind": "cortex-publication-receipts-invalid-container/v1",
            "raw": {"legacy": "opaque"},
            "diagnostic": {"code": "container-not-list"},
        },
        "expected_after_to_dict_and_reload": {
            "kind": "cortex-publication-receipts-invalid-container/v1",
            "raw": {"legacy": "opaque"},
            "diagnostic": {"code": "container-not-list"},
        },
        "append": "conflict",
    },
]


def plan_receipt_for_attempt(attempt: int) -> dict[str, object]:
    snapshot = clone(plan_snapshot)
    snapshot["phase_attempt"] = attempt
    snapshot_ref = (
        f"planning-input-snapshots/run-contract-01/plan/{attempt}/planning-contract.json"
    )
    snapshot_bytes = j(snapshot)
    snapshot_sha = sha(snapshot_bytes)
    acceptance = clone(plan_acceptance)
    acceptance["ref"] = snapshot_ref
    acceptance["sha256"] = snapshot_sha
    acceptance["phase_attempt"] = attempt
    facts = clone(plan_acceptance_facts)
    facts["phase_attempt"] = attempt
    facts_sha = h("cortex-plan-acceptance-facts/v1", facts)
    event_basis = clone(plan_event_basis)
    event_basis["phase_attempt"] = attempt
    event_basis["acceptance_facts_sha256"] = facts_sha
    event_id = h("cortex-self-publication-event/plan/v1", event_basis)
    intent = clone(plan_intent)
    intent["acceptance_evidence"] = acceptance
    intent_bytes = j(intent)
    intent_sha = sha(intent_bytes)
    intent_ref = (
        f"planning-publication-intents/run-contract-01/plan/{event_id}.json"
    )
    publication_basis = {"producer_event_id": event_id, "target": plan_target}
    publication_id = h(
        "cortex-self-publication-publication/plan/v1", publication_basis
    )
    receipt = clone(plan_receipt)
    receipt["producer_event_id"] = event_id
    receipt["publication_id"] = publication_id
    receipt["acceptance_evidence"] = acceptance
    receipt["transaction"]["intent_ref"] = intent_ref
    receipt["transaction"]["intent_sha256"] = intent_sha
    receipt = reseal(receipt)
    return {
        "snapshot": snapshot,
        "snapshot_ref": snapshot_ref,
        "snapshot_stored_bytes_utf8": snapshot_bytes.decode("utf-8"),
        "snapshot_sha256": snapshot_sha,
        "acceptance_facts": facts,
        "acceptance_facts_sha256": facts_sha,
        "event_basis": event_basis,
        "producer_event_id": event_id,
        "intent_core": intent,
        "intent_stored_bytes_utf8": intent_bytes.decode("utf-8"),
        "intent_sha256": intent_sha,
        "intent_ref": intent_ref,
        "publication_basis": publication_basis,
        "publication_id": publication_id,
        "receipt": receipt,
    }


plan_attempt3 = plan_receipt_for_attempt(3)
alternate_same_pair = pr_receipt_with_witness(
    base_pr, 8, 7008, "PR_kwDOExample0002"
)
alternate_same_object = pr_receipt_with_new_metadata(
    base_pr, "feat(workflow): café revised"
)[0]
second_event_basis = clone(pr_event_basis)
second_event_basis["ship_attempt"] = 2
second_event_id = h("cortex-self-publication-event/manager-pr/v1", second_event_basis)
alternate_second_event = clone(base_pr)
alternate_second_event["producer_event_id"] = second_event_id
alternate_second_event = reseal(alternate_second_event)

fixture_patch = {"fixture_patch_token": "coupled-to-brainstorm-event-01"}
two_receipt_batch = {
    "producer_kind": "brainstorm_artifact",
    "producer_event_id": brain_event_id,
    "coupled_run_patch": fixture_patch,
    "receipts": brain_receipts,
}
append_vectors = [
    {
        "id": "brainstorm-two-output-all-new",
        "history": [],
        "incoming_batch": two_receipt_batch,
        "expected": {"outcome": "appended", "persist_attempts": 1, "rows_added": 2},
        "note": "The patch token is harness-only equality data; it is not a #1029 wire member.",
    },
    {
        "id": "brainstorm-exact-full-batch-replay",
        "history": brain_receipts,
        "incoming_batch": two_receipt_batch,
        "expected": {"outcome": "no-op", "persist_attempts": 0, "rows_added": 0},
    },
    {
        "id": "brainstorm-partial-replay-conflicts",
        "history": brain_receipts[:1],
        "incoming_batch": two_receipt_batch,
        "expected": {"outcome": "conflict", "persist_attempts": 0, "rows_added": 0},
    },
    {
        "id": "duplicate-pair-inside-batch-conflicts",
        "history": [],
        "incoming_batch": {
            **two_receipt_batch,
            "receipts": [brain_receipts[0], brain_receipts[0]],
        },
        "expected": {"outcome": "conflict", "persist_attempts": 0, "rows_added": 0},
    },
    {
        "id": "same-pair-different-valid-payload-conflicts",
        "history": [base_pr],
        "incoming_batch": {
            "producer_kind": "manager_pull_request",
            "producer_event_id": pr_event_id,
            "coupled_run_patch": {"fixture_patch_token": "same-manager-event"},
            "receipts": [alternate_same_pair],
        },
        "expected": {"outcome": "conflict", "persist_attempts": 0, "rows_added": 0},
        "note": "Only POST witness and matching GET resource tuple differ; each receipt is internally re-sealed and cross-bound.",
    },
    {
        "id": "same-publication-id-across-events-conflicts",
        "history": [base_pr],
        "incoming_batch": {
            "producer_kind": "manager_pull_request",
            "producer_event_id": second_event_id,
            "coupled_run_patch": {"fixture_patch_token": "ship-attempt-2"},
            "receipts": [alternate_second_event],
        },
        "context": {"second_event_basis": second_event_basis},
        "expected": {"outcome": "conflict", "persist_attempts": 0, "rows_added": 0},
    },
    {
        "id": "same-pr-object-different-publication-conflicts",
        "history": [base_pr],
        "incoming_batch": {
            "producer_kind": "manager_pull_request",
            "producer_event_id": pr_event_id,
            "coupled_run_patch": {"fixture_patch_token": "same-manager-event"},
            "receipts": [alternate_same_object],
        },
        "context": {
            "alternate_intent": pr_receipt_with_new_metadata(
                base_pr, "feat(workflow): café revised"
            )[1],
            "alternate_intent_hashes": pr_receipt_with_new_metadata(
                base_pr, "feat(workflow): café revised"
            )[2],
            "proposed_pr_resource_key": {
                "repository": repo,
                "number": pr_witness["number"],
            },
        },
        "expected": {"outcome": "conflict", "persist_attempts": 0, "rows_added": 0},
    },
    {
        "id": "same-planning-ref-different-event-conflicts",
        "history": [plan_receipt],
        "incoming_batch": {
            "producer_kind": "plan_materialization",
            "producer_event_id": plan_attempt3["producer_event_id"],
            "coupled_run_patch": {"fixture_patch_token": "plan-attempt-3"},
            "receipts": [plan_attempt3["receipt"]],
        },
        "context": {
            **plan_attempt3,
            "proposed_planning_object_key": {
                "run_id": run_id,
                "claim_key": claim_key,
                "producer_kind": "plan_materialization",
                "workspace_ref": plan_target_ref,
            },
            "history_workspace_ref": plan_receipt["published_object"]["ref"],
            "incoming_workspace_ref": plan_attempt3["receipt"]["published_object"]["ref"],
        },
        "expected": {
            "outcome": "conflict",
            "reason": "same-planning-object-key-across-events",
            "persist_attempts": 0,
            "rows_added": 0,
        },
    },
    {
        "id": "opaque-invalid-container-conflicts",
        "history": next(
            row["expected_serialized_field"]
            for row in vectors["negative_vectors"]
            if row.get("id") == "invalid-non-list-container-round-trip"
        ),
        "incoming_batch": two_receipt_batch,
        "expected": {"outcome": "conflict", "persist_attempts": 0, "rows_added": 0},
    },
    {
        "id": "mixed-event-batch-conflicts",
        "history": [],
        "incoming_batch": {
            "producer_kind": "manager_pull_request",
            "producer_event_id": pr_event_id,
            "coupled_run_patch": {"fixture_patch_token": "mixed-event"},
            "receipts": [base_pr, alternate_second_event],
        },
        "expected": {"outcome": "conflict", "persist_attempts": 0, "rows_added": 0},
    },
]
vectors["append_replay_conflict_vectors"] = append_vectors
vectors["pending_vectors"] = [
    {
        "id": "distinct-receipts-with-a-sha256-receipt-id-collision",
        "status": "pending-unconstructible-without-a-cryptographic-collision",
        "reason": "A literal pair of distinct valid receipt preimages with one SHA-256 receipt_id cannot be independently generated; using an invented digest would make the receipt invalid, so this is not presented as an executable golden vector.",
        "required_owner_behavior": "#993 rejects distinct payloads that carry the same receipt_id before mutation.",
    }
]

OUT.write_text(
    json.dumps(vectors, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    + "\n",
    encoding="utf-8",
)
print(f"wrote {OUT}")
for row in vectors["generic_vectors"]:
    print(f"generic H(test/v1, {row['canonical_utf8']}) = {row['sha256']}")
for name, row in vectors["producer_vectors"].items():
    print(
        name,
        "event=", row["event"]["sha256"],
        "publication=", row["publication_id"]["value"],
        "receipt=", row["receipt_id"]["sha256"],
    )
