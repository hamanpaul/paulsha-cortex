from __future__ import annotations

import inspect

import pytest

from paulsha_cortex.coordinator.planning import (
    ArtifactAssessment,
    BlockingMarker,
    CompletenessReport,
    PlanningArtifact,
    QuestionPack,
    SizingScore,
    compute_sizing_score,
)

# #221：五維 sizing 評分（#208 設計 H.1）。三維（acceptance_surfaces/spec_stability/
# orchestration）機械算，二維（domain_breadth/state_consistency）由 plan frontmatter 宣告。


def _plan_artifact(domain_breadth: object = 2, state_consistency: object = 1, extra: str = "") -> PlanningArtifact:
    text = (
        "---\n"
        f"domain_breadth: {domain_breadth}\n"
        f"state_consistency: {state_consistency}\n"
        "status: accepted\n"
        "---\n"
        "## Tasks\n"
        f"{extra}"
    )
    return PlanningArtifact(kind="plan", ref="docs/superpowers/plans/demo.md", text=text)


def _empty_question_pack() -> QuestionPack:
    return QuestionPack(pack_id="qp-empty", questions=())


def _completeness_report(*, missing_kinds=(), blocking=False) -> CompletenessReport:
    artifact = PlanningArtifact(kind="plan", ref="docs/superpowers/plans/demo.md", text="---\nstatus: accepted\n---\n## Tasks\n- a")
    markers = (BlockingMarker("standalone", 5, "TBD"),) if blocking else ()
    assessment = ArtifactAssessment(artifact, not blocking, () if not blocking else ("blocking-decision",), markers)
    return CompletenessReport(
        complete=not missing_kinds and not blocking,
        assessments=(assessment,),
        missing_kinds=tuple(missing_kinds),
        default_question_pack=_empty_question_pack(),
    )


def test_compute_sizing_score_full_example():
    score = compute_sizing_score(
        plan_artifact=_plan_artifact(domain_breadth=2, state_consistency=1),
        completeness_report=_completeness_report(),
        gate_spine_count=2,
        applicable_contract_rules=frozenset({"R-09", "R-16", "R-19"}),
        cards_count=5,
        persona_binding_count=3,
    )
    assert isinstance(score, SizingScore)
    assert score.domain_breadth == 2
    assert score.state_consistency == 1
    assert score.spec_stability == 2  # 全 accepted 無缺無阻塞
    assert score.acceptance_surfaces == 2  # gate 2 + rule 3 = 5 → 上限 2
    assert score.orchestration == 2  # cards>1 且 persona_binding>1
    assert score.total == 9
    assert score.to_dict()["total"] == 9


@pytest.mark.parametrize(
    "missing_kinds, blocking, expected",
    [
        ((), False, 2),
        (("design",), False, 1),
        ((), True, 1),
        (("design",), True, 0),
        (("design", "spec"), False, 0),
    ],
)
def test_spec_stability_grading(missing_kinds, blocking, expected):
    score = compute_sizing_score(
        plan_artifact=_plan_artifact(),
        completeness_report=_completeness_report(missing_kinds=missing_kinds, blocking=blocking),
        gate_spine_count=0,
        applicable_contract_rules=frozenset(),
        cards_count=1,
        persona_binding_count=0,
    )
    assert score.spec_stability == expected


@pytest.mark.parametrize(
    "missing_kinds, blocking, rejected, expected",
    [
        ((), False, False, 0),  # 完整 accepted → 0
        (("design",), False, False, 1),  # 單缺 kind → 1
        (("design", "spec"), False, False, 2),  # 至少兩缺 kind → 2
        ((), True, False, 2),  # marker → 2
        ((), False, True, 2),  # 拒收 artifact → 2
    ],
)
def test_spec_stability_v2_oracle(missing_kinds, blocking, rejected, expected):
    artifact = PlanningArtifact(
        kind="plan",
        ref="docs/superpowers/plans/demo.md",
        text="---\nstatus: " + ("draft" if rejected else "accepted") + "\n---\n## Tasks\n- a",
    )
    markers = (BlockingMarker("standalone", 5, "TBD"),) if blocking else ()
    assessment = ArtifactAssessment(
        artifact,
        not blocking and not rejected,
        () if (not blocking and not rejected) else ("blocking-decision" if blocking else "status-not-accepted",),
        markers,
    )
    report = CompletenessReport(
        complete=not missing_kinds and not blocking and not rejected,
        assessments=(assessment,),
        missing_kinds=tuple(missing_kinds),
        default_question_pack=_empty_question_pack(),
    )
    score = compute_sizing_score(
        plan_artifact=_plan_artifact(),
        completeness_report=report,
        gate_spine_count=0,
        applicable_contract_rules=frozenset(),
        cards_count=1,
        persona_binding_count=0,
    )
    assert score.spec_stability == expected


@pytest.mark.parametrize(
    "gate_spine_count, rules, expected",
    [
        (0, frozenset(), 0),
        (1, frozenset(), 1),
        (0, frozenset({"R-09"}), 1),
        (2, frozenset({"R-09"}), 2),
        (1, frozenset({"R-09", "R-16", "R-19"}), 2),
    ],
)
def test_acceptance_surfaces_grading(gate_spine_count, rules, expected):
    score = compute_sizing_score(
        plan_artifact=_plan_artifact(),
        completeness_report=_completeness_report(),
        gate_spine_count=gate_spine_count,
        applicable_contract_rules=rules,
        cards_count=1,
        persona_binding_count=0,
    )
    assert score.acceptance_surfaces == expected


@pytest.mark.parametrize(
    "cards_count, persona_binding_count, expected",
    [
        (1, 0, 0),
        (1, 1, 0),
        (3, 1, 1),
        (3, 2, 2),
    ],
)
def test_orchestration_grading(cards_count, persona_binding_count, expected):
    score = compute_sizing_score(
        plan_artifact=_plan_artifact(),
        completeness_report=_completeness_report(),
        gate_spine_count=0,
        applicable_contract_rules=frozenset(),
        cards_count=cards_count,
        persona_binding_count=persona_binding_count,
    )
    assert score.orchestration == expected


def test_declared_dimension_missing_rejected():
    text = "---\ndomain_breadth: 1\nstatus: accepted\n---\n## Tasks\n- a"
    artifact = PlanningArtifact(kind="plan", ref="docs/superpowers/plans/demo.md", text=text)
    with pytest.raises(ValueError, match="state_consistency"):
        compute_sizing_score(
            plan_artifact=artifact,
            completeness_report=_completeness_report(),
            gate_spine_count=0,
            applicable_contract_rules=frozenset(),
            cards_count=1,
            persona_binding_count=0,
        )


@pytest.mark.parametrize("yaml_value", ["3", "-1", '"2"', "1.5"])
def test_declared_dimension_out_of_range_or_wrong_type_rejected(yaml_value):
    # 分別涵蓋：超出上界(3)、超出下界(-1)、字串型別("2")、浮點型別(1.5)。
    text = (
        "---\n"
        f"domain_breadth: {yaml_value}\n"
        "state_consistency: 1\n"
        "status: accepted\n"
        "---\n"
        "## Tasks\n"
    )
    artifact = PlanningArtifact(kind="plan", ref="docs/superpowers/plans/demo.md", text=text)
    with pytest.raises(ValueError):
        compute_sizing_score(
            plan_artifact=artifact,
            completeness_report=_completeness_report(),
            gate_spine_count=0,
            applicable_contract_rules=frozenset(),
            cards_count=1,
            persona_binding_count=0,
        )


def test_declared_dimension_rejects_bool_even_though_bool_is_int_subclass():
    with pytest.raises(ValueError, match="domain_breadth"):
        compute_sizing_score(
            plan_artifact=_plan_artifact(domain_breadth=True),
            completeness_report=_completeness_report(),
            gate_spine_count=0,
            applicable_contract_rules=frozenset(),
            cards_count=1,
            persona_binding_count=0,
        )


def test_non_plan_artifact_kind_rejected():
    artifact = PlanningArtifact(kind="design", ref="docs/superpowers/specs/demo-design.md", text="---\n---\n")
    with pytest.raises(ValueError, match="plan"):
        compute_sizing_score(
            plan_artifact=artifact,
            completeness_report=_completeness_report(),
            gate_spine_count=0,
            applicable_contract_rules=frozenset(),
            cards_count=1,
            persona_binding_count=0,
        )


def test_unknown_contract_rule_rejected():
    with pytest.raises(ValueError, match="R-99"):
        compute_sizing_score(
            plan_artifact=_plan_artifact(),
            completeness_report=_completeness_report(),
            gate_spine_count=0,
            applicable_contract_rules=frozenset({"R-99"}),
            cards_count=1,
            persona_binding_count=0,
        )


def test_sizing_score_rejects_out_of_range_dimension_directly():
    with pytest.raises(ValueError):
        SizingScore(
            domain_breadth=3,
            state_consistency=0,
            acceptance_surfaces=0,
            spec_stability=0,
            orchestration=0,
        )


def test_compute_sizing_score_has_no_model_session_params():
    # 驗收條件 3：全程不新增 model session——介面上就不該出現任何
    # registry/probes/questioner 等會啟動 CLI 模型進程的參數。
    sig = inspect.signature(compute_sizing_score)
    forbidden = {
        "registry",
        "probes",
        "primary",
        "primary_questioner",
        "secondary_planner",
        "primary_integrator",
        "artifact_writer",
        "evidence_writer",
    }
    assert not (set(sig.parameters) & forbidden)


def test_compute_sizing_score_is_pure_and_deterministic():
    kwargs = dict(
        plan_artifact=_plan_artifact(),
        completeness_report=_completeness_report(),
        gate_spine_count=1,
        applicable_contract_rules=frozenset({"R-09"}),
        cards_count=2,
        persona_binding_count=1,
    )
    first = compute_sizing_score(**kwargs)
    second = compute_sizing_score(**kwargs)
    assert first == second
