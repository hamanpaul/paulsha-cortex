"""#707：planning 失敗 evidence 必須保存觸發失敗階段的有界模型輸入。"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, planning, work_actions
from paulsha_cortex.coordinator.model_identities import CapabilityProbe, IdentityRegistry


_ACCEPTED_SPEC = "---\nstatus: accepted\n---\n# Spec\n\n## Requirements\n\nBound.\n"


def test_planning_failure_kind_survives_evidence_readback(tmp_path: Path) -> None:
    evidence_ref = manager._write_planning_failure_evidence(
        coordinator_root=tmp_path,
        run_id="workflow-drift-kind",
        classification="environment",
        reason="planning diagnostic text may change",
        failure_kind="operator_worktree_drift",
    )
    run = SimpleNamespace(
        run_id="workflow-drift-kind",
        evidence_refs=(evidence_ref,),
    )

    body = json.loads(Path(evidence_ref).read_text(encoding="utf-8"))
    record = work_actions._read_planning_failure_record(
        run=run, run_id=run.run_id
    )
    hint = work_actions._planning_failure_hint(run)

    assert body["failure_kind"] == "operator_worktree_drift"
    assert record["failure_kind"] == "operator_worktree_drift"
    assert hint is not None
    assert hint["failure_kind"] == "operator_worktree_drift"


def _brainstorm(tmp_path: Path, *, questioner, secondary, integrator):
    report = planning.assess_planning_completeness(
        [planning.PlanningArtifact("spec", "docs/spec.md", _ACCEPTED_SPEC)]
    )
    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "agy",
                "model_id": "secondary",
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": "agy-plan-sandbox",
            },
        ]
    )
    return planning.run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "primary"),
        registry=registry,
        probes={("agy", "secondary"): CapabilityProbe.ready_for("agy", "secondary", "google")},
        evidence_dir=tmp_path / "evidence",
        artifact_root=tmp_path,
        scope=planning.PlanningScope(
            repo="owner/repo", work_id="demo", source_revision="a" * 40
        ),
        primary_questioner=questioner,
        secondary_planner=secondary,
        primary_integrator=integrator,
    )


def _valid_secondary(pack: dict[str, object], _identity) -> dict[str, object]:
    return {
        "schema_version": 1,
        "question_pack_id": pack["pack_id"],
        "evidence": [
            {
                "question_id": question["question_id"],
                "claims": ["No accepted artifact evidence."],
                "source_refs": ["docs/index.md:1"],
            }
            for question in pack["questions"]
        ],
    }


def test_questioner_failure_captures_exact_report_and_default_pack_input(tmp_path: Path) -> None:
    received = []

    def questioner(payload):
        received.append(deepcopy(payload))
        raise RuntimeError("invalid model output")

    result = _brainstorm(
        tmp_path,
        questioner=questioner,
        secondary=lambda *_: pytest.fail("secondary must not run"),
        integrator=lambda *_: pytest.fail("integrator must not run"),
    )

    assert result.model_input == {"stage": "questioner", "args": received}
    assert "default_question_pack" in result.model_input["args"][0]


def test_secondary_failure_captures_its_question_pack_input(tmp_path: Path) -> None:
    received = []

    def secondary(payload, _identity):
        received.append(deepcopy(payload))
        raise RuntimeError("invalid model output")

    result = _brainstorm(
        tmp_path,
        questioner=lambda payload: payload["default_question_pack"],
        secondary=secondary,
        integrator=lambda *_: pytest.fail("integrator must not run"),
    )

    assert result.model_input == {"stage": "secondary", "args": received}
    assert received and received[0]["questions"]


def test_integrator_failure_captures_both_inputs(tmp_path: Path) -> None:
    received = []

    def integrator(question_pack, secondary_evidence):
        received.append([deepcopy(question_pack), deepcopy(secondary_evidence)])
        raise RuntimeError("invalid model output")

    result = _brainstorm(
        tmp_path,
        questioner=lambda payload: payload["default_question_pack"],
        secondary=_valid_secondary,
        integrator=integrator,
    )

    assert result.model_input == {"stage": "integrator", "args": received[0]}
    assert received[0][0]["pack_id"] == received[0][1]["question_pack_id"]
    assert "evidence_hash" in received[0][1]


def test_planning_failure_evidence_stores_guarded_and_bounded_model_input(
    tmp_path: Path,
) -> None:
    model_input = {
        "stage": "questioner",
        "args": [{"prompt": "timeout " + "長" * 5000}],
    }

    evidence_path = manager._write_planning_failure_evidence(
        coordinator_root=tmp_path,
        run_id="workflow-test",
        classification="content",
        reason="question-pack-malformed: invalid model output",
        model_input=model_input,
    )

    body = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    excerpt = body["model_input"]
    assert excerpt["stage"] == "questioner"
    assert excerpt["truncated"] is True
    assert excerpt["content_length_bytes"] > 2048
    assert len(excerpt["input_json"].encode("utf-8")) <= 2048
    assert "_timeout_" in excerpt["input_json"]
