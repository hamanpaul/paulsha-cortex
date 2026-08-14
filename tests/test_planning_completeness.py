from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from paulsha_cortex.coordinator.model_identities import (
    CapabilityProbe,
    IdentityRegistry,
)
from paulsha_cortex.coordinator.planning import (
    PLANNING_KINDS,
    PlanningArtifact,
    PlanningScope,
    _REQUIRED_HEADINGS,
    assess_planning_artifact,
    assess_planning_completeness,
    required_heading_hint,
    run_heterogeneous_brainstorm,
    validate_question_pack,
    validate_secondary_evidence,
)


def _artifact(kind: str, body: str, name: str | None = None) -> PlanningArtifact:
    return PlanningArtifact(kind=kind, ref=name or f"docs/{kind}.md", text=body)


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

## Goal

Deliver the feature.

## Task 1: Implement

Add tests first.
"""

SCOPE = PlanningScope(
    repo="hamanpaul/paulsha-cortex",
    work_id="unified-work-lifecycle",
    source_revision="tree:0123456789abcdef",
)


def test_artifact_acceptance_requires_status_sections_and_no_blocking_marker() -> None:
    accepted = assess_planning_artifact(_artifact("spec", ACCEPTED_SPEC))
    assert accepted.accepted is True

    missing_status = assess_planning_artifact(
        _artifact("design", "# Design\n\n## Decisions\n\nUse one writer.\n")
    )
    assert missing_status.reasons == ("status-not-accepted",)

    missing_section = assess_planning_artifact(
        _artifact("plan", "---\nstatus: accepted\n---\n# Plan\n\nNo executable task.\n")
    )
    assert "required-section-missing" in missing_section.reasons

    duplicate_status = assess_planning_artifact(
        _artifact(
            "spec",
            "---\nstatus: draft\nstatus: accepted\n---\n# Spec\n\n## Requirements\n\nFixed.\n",
        )
    )
    assert "status-not-accepted" in duplicate_status.reasons


def test_required_heading_criterion_is_the_single_source_of_the_prompt_hint() -> None:
    """issue #520：驗收判準（`_REQUIRED_HEADINGS`）與 integrator prompt 的標題要求
    過去是兩份各自維護的真實來源，已因不同步造成 #516／#520 兩次確定性失敗。
    `required_heading_hint()` 必須由判準機械產生：首選標題真的通過驗收、完整可接受
    集合都揭露給模型，且附加 kind 名稱的寫法確實會被拒收。"""
    # 判準本身不因本次重構而改變（frozen expectation）。
    assert _REQUIRED_HEADINGS == {
        "spec": frozenset({"requirements", "requirement", "problem", "problem and outcome", "goals"}),
        "design": frozenset({"decisions", "decision", "design", "architecture"}),
        "plan": frozenset({"task", "tasks"}),
    }

    hint = required_heading_hint()
    for kind in PLANNING_KINDS:
        for title in _REQUIRED_HEADINGS[kind]:
            assert re.search(rf"\b{re.escape(title)}\b", hint.casefold()), title

    for kind, heading in (("spec", "Requirements"), ("design", "Decisions"), ("plan", "Tasks")):
        assert f'exactly "## {heading}" for kind={kind}' in hint
        accepted = assess_planning_artifact(
            _artifact(kind, f"---\nstatus: accepted\n---\n# T\n\n## {heading}\n\nBody.\n")
        )
        assert accepted.accepted is True, kind

    rejected = assess_planning_artifact(
        _artifact("spec", "---\nstatus: accepted\n---\n# T\n\n## Requirements for spec\n\nBody.\n")
    )
    assert "required-section-missing" in rejected.reasons


def test_canonical_accepted_source_spec_and_plan_satisfy_required_sections() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = (root / "docs/superpowers/specs/2026-07-17-unified-work-lifecycle.md").read_text(
        encoding="utf-8"
    )
    plan = (root / "docs/superpowers/plans/2026-07-17-unified-work-lifecycle.md").read_text(
        encoding="utf-8"
    )

    assert assess_planning_artifact(_artifact("spec", spec)).accepted is True
    assert assess_planning_artifact(_artifact("plan", plan)).accepted is True


def test_marker_parser_only_blocks_standalone_or_actual_open_question_items() -> None:
    harmless = ACCEPTED_SPEC + """

The literal text `Decision: TBD` is documented inline and is not a decision.

```markdown
TBD
Decision: TBD
```

## Open Questions

No blocking open question.
"""
    assert assess_planning_artifact(_artifact("spec", harmless)).accepted is True

    for marker in ("TBD", "[TBD]", "Decision: TBD", "決策：未定"):
        result = assess_planning_artifact(_artifact("spec", ACCEPTED_SPEC + f"\n{marker}\n"))
        assert result.accepted is False
        assert result.blocking_markers

    for marker in (
        "- TBD: choose retry owner",
        "- [TBD] choose retry owner",
        "- [ ] Decision: TBD — choose retry owner",
        "1. 決策：未定：選擇 retry owner",
    ):
        result = assess_planning_artifact(_artifact("design", ACCEPTED_DESIGN + f"\n{marker}\n"))
        assert result.accepted is True
        assert not result.blocking_markers

    literal = assess_planning_artifact(
        _artifact("design", ACCEPTED_DESIGN + "\n- TBD is literal documentation, not a decision.\n")
    )
    assert literal.accepted is True

    open_item = ACCEPTED_DESIGN + "\n## Open Questions\n\n- Which provider owns retry?\n"
    result = assess_planning_artifact(_artifact("design", open_item))
    assert result.accepted is False
    assert result.blocking_markers[0].kind == "open-question"


def test_completeness_requires_accepted_spec_design_and_plan() -> None:
    report = assess_planning_completeness(
        [
            _artifact("spec", ACCEPTED_SPEC),
            _artifact("design", ACCEPTED_DESIGN),
        ]
    )

    assert report.complete is False
    assert report.missing_kinds == ("plan",)
    assert [question.kind for question in report.default_question_pack.questions] == ["missing-plan"]


def test_rejected_artifacts_remain_authoritative_sources_for_missing_kind_questions() -> None:
    report = assess_planning_completeness(
        [
            _artifact("spec", "---\nstatus: draft\n---\n## Requirements\nDraft.\n", "openspec/changes/demo/proposal.md"),
            _artifact("design", "---\nstatus: draft\n---\n## Decisions\nDraft.\n", "openspec/changes/demo/design.md"),
            _artifact("plan", "---\nstatus: draft\n---\n## Tasks\n- [ ] Draft.\n", "openspec/changes/demo/tasks.md"),
        ]
    )

    assert [question.source_refs for question in report.default_question_pack.questions] == [
        ("openspec/changes/demo/proposal.md",),
        ("openspec/changes/demo/design.md",),
        ("openspec/changes/demo/tasks.md",),
    ]


def test_missing_kind_questions_fall_back_to_accepted_refs_when_kind_absent() -> None:
    """issue #408（補完）：todo 錨定的 work item（small-fix combo）缺 spec/design
    時，同 kind 完全不存在 → source_refs 過去恆為空，destinations 推導與
    secondary source material 雙雙斷炊。fallback 到 accepted refs 後，
    `_planning_destinations` 能從 workstream todo 路徑導出目的地。"""
    todo_ref = "docs/superpowers/workstreams/fix-demo-v3/todo.md"
    report = assess_planning_completeness(
        [
            _artifact(
                "plan",
                "---\nstatus: accepted\nwork_item: fix-demo-v3\n---\n## Tasks\n- [ ] 修好它。\n",
                todo_ref,
            )
        ]
    )
    assert report.missing_kinds == ("spec", "design")
    for question in report.default_question_pack.questions:
        assert question.source_refs == (todo_ref,)


def test_any_blocking_marker_triggers_brainstorm_even_when_an_alternate_artifact_is_accepted() -> None:
    report = assess_planning_completeness(
        [
            _artifact("spec", ACCEPTED_SPEC, "docs/spec-good.md"),
            _artifact("spec", ACCEPTED_SPEC + "\nTBD\n", "docs/spec-blocked.md"),
            _artifact("design", ACCEPTED_DESIGN),
            _artifact("plan", ACCEPTED_PLAN),
        ]
    )

    assert report.missing_kinds == ()
    assert report.complete is False
    assert [question.kind for question in report.default_question_pack.questions] == [
        "blocking-decision"
    ]


def test_question_pack_and_secondary_output_are_strict_evidence_only() -> None:
    report = assess_planning_completeness([_artifact("spec", ACCEPTED_SPEC)])
    expected = report.default_question_pack
    payload = expected.to_dict()
    pack = validate_question_pack(payload, report=report)
    assert pack.pack_id == expected.pack_id

    evidence = validate_secondary_evidence(
        {
            "schema_version": 1,
            "question_pack_id": pack.pack_id,
            "evidence": [
                {
                    "question_id": question.question_id,
                    "claims": ["Repository source does not contain an accepted artifact."],
                    "source_refs": ["docs/index.md:7"],
                }
                for question in pack.questions
            ],
        },
        question_pack=pack,
    )
    assert len(evidence.items) == len(pack.questions)

    with pytest.raises(ValueError, match="unexpected.*decisions"):
        validate_secondary_evidence(
            {
                "schema_version": 1,
                "question_pack_id": pack.pack_id,
                "evidence": [],
                "decisions": ["secondary must not decide"],
            },
            question_pack=pack,
        )


def test_brainstorm_is_heterogeneous_persists_immutable_peer_evidence_and_keeps_review_gate_empty(
    tmp_path: Path,
) -> None:
    report = assess_planning_completeness([_artifact("spec", ACCEPTED_SPEC)])
    registry = IdentityRegistry.from_rows(
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
    probes = {
        ("agy", "Gemini 3.1 Pro (High)"): CapabilityProbe.ready_for(
            "agy", "Gemini 3.1 Pro (High)", "google"
        )
    }

    def primary_questioner(report_payload):
        return report.default_question_pack.to_dict()

    def secondary_evidence(pack_payload, identity):
        assert identity.independence_domain == "google"
        return {
            "schema_version": 1,
            "question_pack_id": pack_payload["pack_id"],
            "evidence": [
                {
                    "question_id": q["question_id"],
                    "claims": ["No accepted artifact found."],
                    "source_refs": ["docs/planning-index.md:1"],
                }
                for q in pack_payload["questions"]
            ],
        }

    def primary_integrator(pack_payload, evidence_payload):
        kind_to_body = {"spec": ACCEPTED_SPEC, "design": ACCEPTED_DESIGN, "plan": ACCEPTED_PLAN}
        resolutions = []
        for question in pack_payload["questions"]:
            artifact_kind = question["kind"].removeprefix("missing-")
            artifact_ref = f"docs/{artifact_kind}.md"
            path = tmp_path / artifact_ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(kind_to_body[artifact_kind], encoding="utf-8")
            resolutions.append(
                {
                    "question_id": question["question_id"],
                    "decision": "Create and accept the missing artifact.",
                    "artifact_kind": artifact_kind,
                    "artifact_refs": [artifact_ref],
                }
            )
        return {
            "schema_version": 1,
            "question_pack_id": pack_payload["pack_id"],
            "secondary_evidence_hash": evidence_payload["evidence_hash"],
            "resolutions": resolutions,
        }

    original_spec = tmp_path / "docs/spec.md"
    original_spec.parent.mkdir(parents=True, exist_ok=True)
    original_spec.write_text(ACCEPTED_SPEC, encoding="utf-8")

    result = run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "gpt-primary"),
        registry=registry,
        probes=probes,
        evidence_dir=tmp_path,
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=primary_questioner,
        secondary_planner=secondary_evidence,
        primary_integrator=primary_integrator,
    )

    assert result.state == "ready"
    assert result.secondary_domain == "google"
    assert result.gate_refs.brainstorm_peer
    assert result.gate_refs.foreign_review is None
    assert result.gate_refs.copilot is None
    evidence_path = Path(result.gate_refs.brainstorm_peer.ref)
    assert evidence_path.is_file()
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert persisted["kind"] == "brainstorm-peer"
    assert persisted["scope"] == {
        "repo": "hamanpaul/paulsha-cortex",
        "source_revision": "tree:0123456789abcdef",
        "work_id": "unified-work-lifecycle",
    }
    assert persisted["secondary_identity"]["independence_domain"] == "google"
    assert {row["ref"] for row in persisted["artifacts"]} == {
        "docs/spec.md",
        "docs/design.md",
        "docs/plan.md",
    }
    assert all(len(row["sha256"]) == 64 for row in persisted["artifacts"])

    repeated = run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "gpt-primary"),
        registry=registry,
        probes=probes,
        evidence_dir=tmp_path,
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=primary_questioner,
        secondary_planner=secondary_evidence,
        primary_integrator=primary_integrator,
    )
    assert repeated.gate_refs.brainstorm_peer == result.gate_refs.brainstorm_peer

    def conflicting_secondary(pack_payload, identity):
        payload = secondary_evidence(pack_payload, identity)
        payload["evidence"][0]["claims"] = ["Conflicting observation."]
        return payload

    conflict = run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "gpt-primary"),
        registry=registry,
        probes=probes,
        evidence_dir=tmp_path,
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=primary_questioner,
        secondary_planner=conflicting_secondary,
        primary_integrator=primary_integrator,
    )
    assert (conflict.state, conflict.reason) == (
        "needs_human",
        "brainstorm-evidence-conflict",
    )

    other_work = run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "gpt-primary"),
        registry=registry,
        probes=probes,
        evidence_dir=tmp_path,
        artifact_root=tmp_path,
        scope=PlanningScope(
            repo=SCOPE.repo,
            work_id="another-work-item",
            source_revision=SCOPE.source_revision,
        ),
        primary_questioner=primary_questioner,
        secondary_planner=secondary_evidence,
        primary_integrator=primary_integrator,
    )
    assert other_work.state == "ready"
    assert other_work.gate_refs.brainstorm_peer != result.gate_refs.brainstorm_peer

    unsafe_report = assess_planning_completeness(
        [_artifact("spec", ACCEPTED_SPEC, "../outside-root.md")]
    )
    unsafe = run_heterogeneous_brainstorm(
        report=unsafe_report,
        primary=("codex", "gpt-primary"),
        registry=registry,
        probes=probes,
        evidence_dir=tmp_path / "unsafe-ref",
        artifact_root=tmp_path,
        scope=PlanningScope(
            repo=SCOPE.repo,
            work_id="unsafe-ref",
            source_revision=SCOPE.source_revision,
        ),
        primary_questioner=lambda _: unsafe_report.default_question_pack.to_dict(),
        secondary_planner=secondary_evidence,
        primary_integrator=primary_integrator,
    )
    assert (unsafe.state, unsafe.reason) == ("needs_human", "primary-artifact-invalid")


def test_brainstorm_fails_closed_when_no_heterogeneous_peer_or_output_is_malformed(tmp_path: Path) -> None:
    report = assess_planning_completeness([_artifact("spec", ACCEPTED_SPEC)])
    same_domain_registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            }
        ]
    )
    no_peer = run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "primary"),
        registry=same_domain_registry,
        probes={},
        evidence_dir=tmp_path,
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=lambda _: report.default_question_pack.to_dict(),
        secondary_planner=lambda *_: {},
        primary_integrator=lambda *_: {},
    )
    assert (no_peer.state, no_peer.reason) == ("needs_human", "no-heterogeneous-planner")

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
                "model_id": "Gemini 3.1 Pro (High)",
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": "agy-plan-sandbox",
            },
        ]
    )
    malformed = run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "primary"),
        registry=registry,
        probes={
            ("agy", "Gemini 3.1 Pro (High)"): CapabilityProbe.ready_for(
                "agy", "Gemini 3.1 Pro (High)", "google"
            )
        },
        evidence_dir=tmp_path,
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=lambda _: report.default_question_pack.to_dict(),
        secondary_planner=lambda *_: {"schema_version": 1, "evidence": []},
        primary_integrator=lambda *_: {},
    )
    assert malformed.state == "needs_human"
    # Issue #397：reason 不再是壓平後的單一字面值，必須透傳底層例外型別與
    # 訊息片段（`validate_secondary_evidence` 這裡是 ValueError: secondary
    # evidence identity invalid），排障時才看得出真正壞在哪一段驗證。
    assert malformed.reason is not None
    assert malformed.reason.startswith("secondary-output-malformed:")
    assert "ValueError" in malformed.reason
    assert malformed.gate_refs.brainstorm_peer is None

    valid_secondary = lambda pack, _identity: {
        "schema_version": 1,
        "question_pack_id": pack["pack_id"],
        "evidence": [
            {
                "question_id": question["question_id"],
                "claims": ["Missing."],
                "source_refs": ["docs/index.md:1"],
            }
            for question in pack["questions"]
        ],
    }
    malformed_primary = run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "primary"),
        registry=registry,
        probes={
            ("agy", "Gemini 3.1 Pro (High)"): CapabilityProbe.ready_for(
                "agy", "Gemini 3.1 Pro (High)", "google"
            )
        },
        evidence_dir=tmp_path / "primary-malformed",
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=lambda _: report.default_question_pack.to_dict(),
        secondary_planner=valid_secondary,
        primary_integrator=lambda *_: {},
    )
    assert malformed_primary.state == "needs_human"
    # 同上：integrator 分支同型 `except Exception` 也要透傳例外摘要。
    assert malformed_primary.reason is not None
    assert malformed_primary.reason.startswith("primary-integration-malformed:")
    assert "ValueError" in malformed_primary.reason


def test_brainstorm_reason_includes_underlying_exception_summary_for_questioner(
    tmp_path: Path,
) -> None:
    """Issue #397：`question-pack-malformed` 分支同樣要透傳底層例外——過去
    三處 `except Exception` 全部把例外壓平成單一字面值，操作者只看得到分支
    名稱、看不到底層是 ValueError 還是別的、訊息內容是什麼，排障要另外重跑
    加 print 才查得到。這裡直接讓 primary_questioner raise，驗證 reason 併入
    例外型別與訊息片段（供 #393 的 planning-failure evidence／recover-planning
    的 `_read_planning_failure_record` 直接讀出，兩者都只要求非空字串）。"""
    report = assess_planning_completeness([_artifact("spec", ACCEPTED_SPEC)])
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
                "model_id": "Gemini 3.1 Pro (High)",
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": "agy-plan-sandbox",
            },
        ]
    )
    probes = {
        ("agy", "Gemini 3.1 Pro (High)"): CapabilityProbe.ready_for(
            "agy", "Gemini 3.1 Pro (High)", "google"
        )
    }

    def raising_questioner(_input: object) -> object:
        raise RuntimeError("questioner exploded")

    result = run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "primary"),
        registry=registry,
        probes=probes,
        evidence_dir=tmp_path,
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=raising_questioner,
        secondary_planner=lambda *_: {},
        primary_integrator=lambda *_: {},
    )
    assert result.state == "needs_human"
    assert result.reason is not None
    assert result.reason.startswith("question-pack-malformed:")
    assert "RuntimeError" in result.reason
    assert "questioner exploded" in result.reason


def test_brainstorm_reason_includes_underlying_exception_summary_for_artifact_write(
    tmp_path: Path,
) -> None:
    """Issue #408：artifact-write 分支是 #397 儀器化時漏掉的第四個裸吞
    `except Exception`——canary v2 gen3 只看得到 `primary-artifact-write-rejected`
    卻查不到是哪條驗證拒的。這裡讓整條 brainstorm 走到 artifact_writer，
    驗證 reason 併入例外型別與訊息片段。"""
    report = assess_planning_completeness([_artifact("spec", ACCEPTED_SPEC)])
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
                "model_id": "Gemini 3.1 Pro (High)",
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": "agy-plan-sandbox",
            },
        ]
    )
    probes = {
        ("agy", "Gemini 3.1 Pro (High)"): CapabilityProbe.ready_for(
            "agy", "Gemini 3.1 Pro (High)", "google"
        )
    }

    def questioner(_report_payload):
        return report.default_question_pack.to_dict()

    def secondary(pack_payload, identity):
        return {
            "schema_version": 1,
            "question_pack_id": pack_payload["pack_id"],
            "evidence": [
                {
                    "question_id": q["question_id"],
                    "claims": ["No accepted artifact found."],
                    "source_refs": ["docs/planning-index.md:1"],
                }
                for q in pack_payload["questions"]
            ],
        }

    kind_to_body = {"design": ACCEPTED_DESIGN, "plan": ACCEPTED_PLAN}

    def integrator(pack_payload, evidence_payload):
        resolutions = []
        artifacts = []
        for question in pack_payload["questions"]:
            artifact_kind = question["kind"].removeprefix("missing-")
            artifact_ref = f"docs/{artifact_kind}.md"
            resolutions.append(
                {
                    "question_id": question["question_id"],
                    "decision": "Create and accept the missing artifact.",
                    "artifact_kind": artifact_kind,
                    "artifact_refs": [artifact_ref],
                }
            )
            artifacts.append(
                {"kind": artifact_kind, "path": artifact_ref, "content": kind_to_body[artifact_kind]}
            )
        return {
            "schema_version": 1,
            "question_pack_id": pack_payload["pack_id"],
            "secondary_evidence_hash": evidence_payload["evidence_hash"],
            "resolutions": resolutions,
            "artifacts": artifacts,
        }

    def exploding_writer(_rows):
        raise RuntimeError("publication transaction refused")

    result = run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "primary"),
        registry=registry,
        probes=probes,
        evidence_dir=tmp_path,
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=questioner,
        secondary_planner=secondary,
        primary_integrator=integrator,
        artifact_writer=exploding_writer,
    )
    assert result.state == "needs_human"
    assert result.reason is not None
    assert result.reason.startswith("primary-artifact-write-rejected:")
    assert "RuntimeError" in result.reason
    assert "publication transaction refused" in result.reason


def test_primary_must_write_accepted_artifacts_before_brainstorm_gate_passes(tmp_path: Path) -> None:
    report = assess_planning_completeness([_artifact("spec", ACCEPTED_SPEC)])
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
                "model_id": "Gemini 3.1 Pro (High)",
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": "agy-plan-sandbox",
            },
        ]
    )

    def secondary(pack, _identity):
        return {
            "schema_version": 1,
            "question_pack_id": pack["pack_id"],
            "evidence": [
                {
                    "question_id": question["question_id"],
                    "claims": ["Missing."],
                    "source_refs": ["docs/index.md:1"],
                }
                for question in pack["questions"]
            ],
        }

    def integration(pack, evidence):
        return {
            "schema_version": 1,
            "question_pack_id": pack["pack_id"],
            "secondary_evidence_hash": evidence["evidence_hash"],
            "resolutions": [
                {
                    "question_id": question["question_id"],
                    "decision": "Claim a file was written.",
                    "artifact_kind": question["kind"].removeprefix("missing-"),
                    "artifact_refs": ["docs/does-not-exist.md"],
                }
                for question in pack["questions"]
            ],
        }

    result = run_heterogeneous_brainstorm(
        report=report,
        primary=("codex", "primary"),
        registry=registry,
        probes={
            ("agy", "Gemini 3.1 Pro (High)"): CapabilityProbe.ready_for(
                "agy", "Gemini 3.1 Pro (High)", "google"
            )
        },
        evidence_dir=tmp_path / "evidence",
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=lambda _: report.default_question_pack.to_dict(),
        secondary_planner=secondary,
        primary_integrator=integration,
    )

    assert (result.state, result.reason) == ("needs_human", "primary-artifact-invalid")
    assert not (tmp_path / "evidence").exists()

    blocked_path = tmp_path / "docs/spec-blocked.md"
    blocked_path.parent.mkdir(parents=True, exist_ok=True)
    blocked_path.write_text(ACCEPTED_SPEC + "\nTBD\n", encoding="utf-8")
    blocked_report = assess_planning_completeness(
        [
            _artifact("spec", ACCEPTED_SPEC + "\nTBD\n", "docs/spec-blocked.md"),
            _artifact("design", ACCEPTED_DESIGN),
            _artifact("plan", ACCEPTED_PLAN),
        ]
    )

    def leaves_original_blocker(pack, evidence):
        replacement = tmp_path / "docs/spec-new.md"
        replacement.parent.mkdir(parents=True, exist_ok=True)
        replacement.write_text(ACCEPTED_SPEC, encoding="utf-8")
        return {
            "schema_version": 1,
            "question_pack_id": pack["pack_id"],
            "secondary_evidence_hash": evidence["evidence_hash"],
            "resolutions": [
                {
                    "question_id": question["question_id"],
                    "decision": "Write a different accepted spec.",
                    "artifact_kind": "spec",
                    "artifact_refs": ["docs/spec-new.md"],
                }
                for question in pack["questions"]
            ],
        }

    blocker_result = run_heterogeneous_brainstorm(
        report=blocked_report,
        primary=("codex", "primary"),
        registry=registry,
        probes={
            ("agy", "Gemini 3.1 Pro (High)"): CapabilityProbe.ready_for(
                "agy", "Gemini 3.1 Pro (High)", "google"
            )
        },
        evidence_dir=tmp_path / "blocker-evidence",
        artifact_root=tmp_path,
        scope=SCOPE,
        primary_questioner=lambda _: blocked_report.default_question_pack.to_dict(),
        secondary_planner=secondary,
        primary_integrator=leaves_original_blocker,
    )
    assert (blocker_result.state, blocker_result.reason) == (
        "needs_human",
        "primary-artifact-invalid",
    )
