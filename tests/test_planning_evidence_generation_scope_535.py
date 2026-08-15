"""issue #535：abandon 後的 planning evidence 不得佔住下一世代的命名空間。

生產現場：前一世代（`workflow-88d089d71416a754dda8`）的 brainstorm evidence 落
在 `evidence/planning/brainstorm-<hash>.json`，run 被 abandon 之後檔案留著；下一
世代重跑 brainstorm，scope 與 question pack 都相同 → 檔名相同，但模型輸出語意
相同而 byte 不同 → `publish()` 的 no-clobber fail-closed 直接把新世代打死。

修法採 issue 建議 (b)：命名空間帶 run identity。evidence 不搬不刪（審計不可變），
前代檔案原位保留、原路徑仍可稽核，新世代自然不撞。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.model_identities import CapabilityProbe, IdentityRegistry
from paulsha_cortex.coordinator.planning import (
    PlanningArtifact,
    PlanningScope,
    assess_planning_completeness,
    brainstorm_evidence_filename,
    run_heterogeneous_brainstorm,
)

GEN_ONE = "workflow-88d089d71416a754dda8"
GEN_TWO = "workflow-7a430d31eff66ef13630"

SCOPE = PlanningScope(
    repo="hamanpaul/paulsha-cortex",
    work_id="fix-instance-config-isolation",
    source_revision="tree:0123456789abcdef",
)

ACCEPTED_SPEC = """\
---
status: accepted
---
# Feature specification

## Requirements

The behavior is fixed.
"""

ACCEPTED_DESIGN = """\
---
status: accepted
---
# Feature design

## Decisions

Use one durable writer.
"""

ACCEPTED_PLAN = """\
---
status: accepted
---
# Feature plan

## Tasks

- [ ] Task 1: implement
"""


def _registry() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "agy",
                "model_id": "Gemini 3.1 Pro (High)",
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": "agy-plan-sandbox",
            },
        ]
    )


_PROBES = {
    ("agy", "Gemini 3.1 Pro (High)"): CapabilityProbe.ready_for(
        "agy", "Gemini 3.1 Pro (High)", "google"
    )
}


def _brainstorm(tmp_path: Path, *, claim: str, run_id: str | None):
    """跑一次 brainstorm；`claim` 模擬「語意相同、措辭不同」的模型輸出。"""

    report = assess_planning_completeness(
        [PlanningArtifact(kind="spec", ref="docs/spec.md", text=ACCEPTED_SPEC)]
    )
    existing_spec = tmp_path / "docs" / "spec.md"
    existing_spec.parent.mkdir(parents=True, exist_ok=True)
    existing_spec.write_text(ACCEPTED_SPEC, encoding="utf-8")

    def primary_questioner(_payload):
        return report.default_question_pack.to_dict()

    def secondary_planner(pack_payload, identity):
        return {
            "schema_version": 1,
            "question_pack_id": pack_payload["pack_id"],
            "evidence": [
                {
                    "question_id": question["question_id"],
                    "claims": [claim],
                    "source_refs": ["docs/planning-index.md:1"],
                }
                for question in pack_payload["questions"]
            ],
        }

    def primary_integrator(pack_payload, evidence_payload):
        bodies = {"spec": ACCEPTED_SPEC, "design": ACCEPTED_DESIGN, "plan": ACCEPTED_PLAN}
        resolutions = []
        for question in pack_payload["questions"]:
            kind = question["kind"].removeprefix("missing-")
            ref = f"docs/{kind}.md"
            target = tmp_path / ref
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(bodies[kind], encoding="utf-8")
            resolutions.append(
                {
                    "question_id": question["question_id"],
                    "decision": "Create and accept the missing artifact.",
                    "artifact_kind": kind,
                    "artifact_refs": [ref],
                }
            )
        return {
            "schema_version": 1,
            "question_pack_id": pack_payload["pack_id"],
            "secondary_evidence_hash": evidence_payload["evidence_hash"],
            "resolutions": resolutions,
        }

    return run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "gpt-primary"),
        registry=_registry(),
        probes=_PROBES,
        evidence_dir=tmp_path / "evidence" / "planning",
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=primary_questioner,
        secondary_planner=secondary_planner,
        primary_integrator=primary_integrator,
        run_id=run_id,
    )


def test_evidence_filename_is_scoped_by_run_identity() -> None:
    pack_id = "pack-0123456789abcdef"
    first = brainstorm_evidence_filename(
        scope=SCOPE, question_pack_id=pack_id, run_id=GEN_ONE
    )
    second = brainstorm_evidence_filename(
        scope=SCOPE, question_pack_id=pack_id, run_id=GEN_TWO
    )
    assert first != second
    assert first.startswith(f"brainstorm-{GEN_ONE}-")
    assert second.startswith(f"brainstorm-{GEN_TWO}-")
    # 同一 run 內必須完全穩定（冪等重跑仍落同一個檔）。
    assert first == brainstorm_evidence_filename(
        scope=SCOPE, question_pack_id=pack_id, run_id=GEN_ONE
    )
    # 未帶 run identity 時退回舊命名，既有殘留檔仍可讀。
    legacy = brainstorm_evidence_filename(scope=SCOPE, question_pack_id=pack_id)
    assert legacy.startswith("brainstorm-") and GEN_ONE not in legacy
    # run_id 也進 hash：改檔名偽造不出同一份 content address。
    assert legacy.removeprefix("brainstorm-") != first.removeprefix(
        f"brainstorm-{GEN_ONE}-"
    )


def test_next_generation_does_not_collide_with_abandoned_generation(
    tmp_path: Path,
) -> None:
    first = _brainstorm(tmp_path, claim="No accepted artifact found.", run_id=GEN_ONE)
    assert first.state == "ready"
    stale = Path(first.gate_refs.brainstorm_peer.ref)
    stale_bytes = stale.read_bytes()

    # 前一世代 abandon；evidence 依審計不可變原則原位保留（不搬、不刪）。
    second = _brainstorm(
        tmp_path,
        claim="The repository has no accepted planning artifact.",
        run_id=GEN_TWO,
    )

    assert second.state == "ready", second.reason
    fresh = Path(second.gate_refs.brainstorm_peer.ref)
    assert fresh != stale
    assert stale.is_file() and stale.read_bytes() == stale_bytes
    assert json.loads(fresh.read_text(encoding="utf-8"))["kind"] == "brainstorm-peer"


def test_same_generation_still_fails_closed_on_divergent_content(
    tmp_path: Path,
) -> None:
    """世代內的衝突偵測不得被放寬——同一個 run 兩次輸出不同仍必須 fail closed。"""

    first = _brainstorm(tmp_path, claim="No accepted artifact found.", run_id=GEN_ONE)
    assert first.state == "ready"
    conflict = _brainstorm(
        tmp_path, claim="A completely different observation.", run_id=GEN_ONE
    )
    assert (conflict.state, conflict.reason) == (
        "needs_human",
        "brainstorm-evidence-conflict",
    )


def test_unscoped_naming_reproduces_the_cross_generation_collision(
    tmp_path: Path,
) -> None:
    """釘住根因：不帶 run identity 時，下一世代必然撞上前代殘留（#535 現場）。"""

    first = _brainstorm(tmp_path, claim="No accepted artifact found.", run_id=None)
    assert first.state == "ready"
    second = _brainstorm(
        tmp_path,
        claim="The repository has no accepted planning artifact.",
        run_id=None,
    )
    assert (second.state, second.reason) == (
        "needs_human",
        "brainstorm-evidence-conflict",
    )


def test_no_clobber_conflict_message_names_the_owning_run(tmp_path: Path) -> None:
    """#535 建議 3：衝突訊息要直接說出殘留檔屬於哪個 run，免得 operator 挖 mtime。"""

    root = tmp_path / "workspace"
    journal_root = tmp_path / "coordinator" / "evidence"
    (root).mkdir(parents=True, exist_ok=True)
    evidence_dir = journal_root / "planning"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    residue = evidence_dir / f"brainstorm-{GEN_ONE}-{'0' * 32}.json"
    residue.write_bytes(b'{"kind": "brainstorm-peer"}\n')

    transaction = manager._PlanningPublicationTransaction(
        root=root,
        run_id=GEN_TWO,
        journal_root=journal_root,
    )
    with pytest.raises(ValueError) as error:
        transaction.publish(
            residue, b"different bytes\n", baseline_hash=None, kind="evidence"
        )

    message = str(error.value)
    assert "planning artifact no-clobber conflict" in message
    assert f"existing owner={GEN_ONE}" in message
    assert f"publishing run={GEN_TWO}" in message
