from __future__ import annotations

import pytest

from paulsha_cortex.coordinator.planning import (
    ArtifactAssessment,
    BlockingMarker,
    CompletenessReport,
    PlanningArtifact,
    QuestionPack,
    assess_planning_completeness,
    compute_sizing_score,
)


def _make_artifact(kind: str, status: str = "accepted", extra: str = "", ref: str | None = None) -> PlanningArtifact:
    if ref is None:
        ref = f"docs/superpowers/{kind}s/demo-{kind}.md" if kind != "plan" else "docs/superpowers/plans/demo.md"
    headings = {
        "spec": "## Requirements\n- R1\n",
        "design": "## Decisions\n- D1\n",
        "plan": "## Tasks\n- [ ] T1\n",
    }
    fm_lines = ["---", f"status: {status}"]
    if kind == "plan":
        fm_lines.extend(["domain_breadth: 1", "state_consistency: 1"])
    fm_lines.append("---\n")
    frontmatter = "\n".join(fm_lines)
    body = headings.get(kind, "## Section\n") + extra
    return PlanningArtifact(kind=kind, ref=ref, text=frontmatter + body)


def _empty_question_pack() -> QuestionPack:
    return QuestionPack(pack_id="qp-empty", questions=())


def _sample_plan_artifact() -> PlanningArtifact:
    return _make_artifact("plan")


def test_stability_direction_full_accepted_triad_is_zero_risk():
    # R1/R2: fully accepted triad with no blockers -> stability risk=0
    artifacts = [
        _make_artifact("spec"),
        _make_artifact("design"),
        _make_artifact("plan"),
    ]
    report = assess_planning_completeness(artifacts)
    assert report.complete is True
    assert len(report.missing_kinds) == 0

    score = compute_sizing_score(
        plan_artifact=_sample_plan_artifact(),
        completeness_report=report,
        gate_spine_count=1,
        applicable_contract_rules=frozenset({"R-09"}),
        cards_count=1,
        persona_binding_count=0,
    )
    # RED expectation: old algorithm computes max(0, 2 - 0 - 0) = 2; new specification requires 0.
    assert score.spec_stability == 0


def test_stability_direction_single_missing_kind_is_one_risk():
    # R2: single missing kind with no other blockers/rejections -> stability risk=1
    artifacts = [
        _make_artifact("spec"),
        _make_artifact("plan"),
    ]
    report = assess_planning_completeness(artifacts)
    assert report.missing_kinds == ("design",)

    score = compute_sizing_score(
        plan_artifact=_sample_plan_artifact(),
        completeness_report=report,
        gate_spine_count=1,
        applicable_contract_rules=frozenset({"R-09"}),
        cards_count=1,
        persona_binding_count=0,
    )
    assert score.spec_stability == 1


def test_stability_direction_multiple_missing_kinds_is_two_risk():
    # R2: at least two missing kinds -> stability risk=2
    artifacts = [
        _make_artifact("plan"),
    ]
    report = assess_planning_completeness(artifacts)
    assert set(report.missing_kinds) == {"spec", "design"}

    score = compute_sizing_score(
        plan_artifact=_sample_plan_artifact(),
        completeness_report=report,
        gate_spine_count=1,
        applicable_contract_rules=frozenset({"R-09"}),
        cards_count=1,
        persona_binding_count=0,
    )
    # RED expectation: old algorithm computes max(0, 2 - 2) = 0; new specification requires 2.
    assert score.spec_stability == 2


def test_stability_direction_blocking_marker_is_two_risk():
    # R2/D1: any blocking marker -> stability risk=2
    spec_with_blocker = _make_artifact(
        "spec",
        extra="\nTBD\n",
    )
    artifacts = [
        spec_with_blocker,
        _make_artifact("design"),
        _make_artifact("plan"),
    ]
    report = assess_planning_completeness(artifacts)
    assert any(a.blocking_markers for a in report.assessments)

    score = compute_sizing_score(
        plan_artifact=_sample_plan_artifact(),
        completeness_report=report,
        gate_spine_count=1,
        applicable_contract_rules=frozenset({"R-09"}),
        cards_count=1,
        persona_binding_count=0,
    )
    # RED expectation: old algorithm computes max(0, 2 - 0 - 1) = 1; new specification requires 2.
    assert score.spec_stability == 2


def test_stability_direction_rejected_artifact_is_two_risk():
    # R2/D1: presence of unaccepted/rejected artifact -> stability risk=2
    rejected_spec = _make_artifact("spec", status="draft")
    artifacts = [
        rejected_spec,
        _make_artifact("design"),
        _make_artifact("plan"),
    ]
    report = assess_planning_completeness(artifacts)
    assert any(not a.accepted for a in report.assessments)

    score = compute_sizing_score(
        plan_artifact=_sample_plan_artifact(),
        completeness_report=report,
        gate_spine_count=1,
        applicable_contract_rules=frozenset({"R-09"}),
        cards_count=1,
        persona_binding_count=0,
    )
    # RED expectation: old algorithm gives 1; new specification requires 2.
    assert score.spec_stability == 2


def test_stability_direction_monotonicity_property():
    # R1: increasing defects must never decrease risk score (risk monotonicity)
    plan_art = _sample_plan_artifact()

    # Level 0: Clean complete triad
    report_clean = assess_planning_completeness([
        _make_artifact("spec"),
        _make_artifact("design"),
        _make_artifact("plan"),
    ])

    # Level 1: 1 missing kind
    report_1_missing = assess_planning_completeness([
        _make_artifact("spec"),
        _make_artifact("plan"),
    ])

    # Level 2: 2 missing kinds
    report_2_missing = assess_planning_completeness([
        _make_artifact("plan"),
    ])

    # Level 3: 2 missing kinds + blocker on plan
    plan_with_blocker = _make_artifact("plan", extra="\nTBD\n")
    report_2_missing_blocker = assess_planning_completeness([
        plan_with_blocker,
    ])

    s0 = compute_sizing_score(plan_artifact=plan_art, completeness_report=report_clean, gate_spine_count=0, applicable_contract_rules=frozenset(), cards_count=1, persona_binding_count=0)
    s1 = compute_sizing_score(plan_artifact=plan_art, completeness_report=report_1_missing, gate_spine_count=0, applicable_contract_rules=frozenset(), cards_count=1, persona_binding_count=0)
    s2 = compute_sizing_score(plan_artifact=plan_art, completeness_report=report_2_missing, gate_spine_count=0, applicable_contract_rules=frozenset(), cards_count=1, persona_binding_count=0)
    s3 = compute_sizing_score(plan_artifact=plan_art, completeness_report=report_2_missing_blocker, gate_spine_count=0, applicable_contract_rules=frozenset(), cards_count=1, persona_binding_count=0)

    # Monotonicity check: risk must be non-decreasing
    # In old code: s0.spec_stability=2, s1.spec_stability=1, s2.spec_stability=0 (strictly decreasing!).
    assert s0.spec_stability <= s1.spec_stability, f"Clean ({s0.spec_stability}) must be <= 1-missing ({s1.spec_stability})"
    assert s1.spec_stability <= s2.spec_stability, f"1-missing ({s1.spec_stability}) must be <= 2-missing ({s2.spec_stability})"
    assert s2.spec_stability <= s3.spec_stability, f"2-missing ({s2.spec_stability}) must be <= 2-missing+blocker ({s3.spec_stability})"


def test_stability_direction_duplicate_kind_with_unqualified_artifact():
    # D1: duplicate unqualified artifact of same kind cannot be masked by accepted one
    spec_ok = _make_artifact("spec", ref="docs/superpowers/specs/spec-a.md")
    spec_unqualified = _make_artifact("spec", status="draft", extra="\nTBD\n", ref="docs/superpowers/specs/spec-b.md")
    design_ok = _make_artifact("design")
    plan_ok = _make_artifact("plan")

    report = assess_planning_completeness([spec_ok, spec_unqualified, design_ok, plan_ok])
    assert "spec" not in report.missing_kinds
    assert any(not a.accepted for a in report.assessments)

    score = compute_sizing_score(
        plan_artifact=_sample_plan_artifact(),
        completeness_report=report,
        gate_spine_count=0,
        applicable_contract_rules=frozenset(),
        cards_count=1,
        persona_binding_count=0,
    )
    # D1: any assessment rejected not due to simply missing kind -> 2.
    # Old code sees missing_kinds=() and blocking_penalty=1 -> computes 1.
    # RED expectation: new specification requires 2.
    assert score.spec_stability == 2


def test_stability_direction_empty_artifacts_unknown_conservatively_two():
    # R2/D1: all 3 kinds missing -> conservative risk 2
    report = assess_planning_completeness([])
    assert set(report.missing_kinds) == {"spec", "design", "plan"}

    score = compute_sizing_score(
        plan_artifact=_sample_plan_artifact(),
        completeness_report=report,
        gate_spine_count=0,
        applicable_contract_rules=frozenset(),
        cards_count=1,
        persona_binding_count=0,
    )
    # RED expectation: old algorithm computes max(0, 2 - 3) = 0 (claiming lowest risk when everything missing!).
    # New specification requires conservative 2.
    assert score.spec_stability == 2


def test_stability_direction_crafted_impossible_report_fails_closed():
    # D1: crafted impossible report with empty assessments claiming empty missing_kinds
    report_clean = assess_planning_completeness([
        _make_artifact("spec"),
        _make_artifact("design"),
        _make_artifact("plan"),
    ])
    score_clean = compute_sizing_score(
        plan_artifact=_sample_plan_artifact(),
        completeness_report=report_clean,
        gate_spine_count=0,
        applicable_contract_rules=frozenset(),
        cards_count=1,
        persona_binding_count=0,
    )

    fake_empty_report = CompletenessReport(
        complete=True,
        assessments=(),
        missing_kinds=(),
        default_question_pack=_empty_question_pack(),
    )
    score_fake = compute_sizing_score(
        plan_artifact=_sample_plan_artifact(),
        completeness_report=fake_empty_report,
        gate_spine_count=0,
        applicable_contract_rules=frozenset(),
        cards_count=1,
        persona_binding_count=0,
    )
    # D1: unknown / impossible report must never be graded as 0 risk, and cannot tie with clean triad.
    # Old code computes max(0, 2-0-0)=2 for BOTH clean and fake (both get 2).
    # In new code: clean is 0, fake is 2. So score_fake.spec_stability > score_clean.spec_stability.
    # RED expectation: fails on old code where 2 > 2 is False!
    assert score_fake.spec_stability > score_clean.spec_stability
    assert score_fake.spec_stability == 2


def test_stability_direction_invalid_domain_or_state_raises_value_error():
    # R4/D2: missing/invalid domain or state in pure function still raises ValueError
    invalid_plan = PlanningArtifact(
        kind="plan",
        ref="docs/superpowers/plans/demo.md",
        text="---\ndomain_breadth: 99\nstate_consistency: 1\nstatus: accepted\n---\n## Tasks\n- a",
    )
    report = assess_planning_completeness([_make_artifact("spec"), _make_artifact("design"), invalid_plan])
    with pytest.raises(ValueError, match="domain_breadth"):
        compute_sizing_score(
            plan_artifact=invalid_plan,
            completeness_report=report,
            gate_spine_count=0,
            applicable_contract_rules=frozenset(),
            cards_count=1,
            persona_binding_count=0,
        )
