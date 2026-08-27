from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.claim import (
    ClaimCandidate,
    WorkAuthority,
    _resume_decision,
)
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo


REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_SLUG = "planning-artifact-manifest-binding"
CHANGE = TASK_SLUG


def _fix_standard_manifest_outputs() -> tuple[str, ...]:
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "fix-standard.yaml", cards)
    result = compile_combo(
        combo,
        cards,
        TASK_SLUG,
        change=CHANGE,
        allow_external=True,
        repo_root=REPO_ROOT,
    )
    assert result.workflow_manifest is not None
    return tuple(
        output
        for step in result.workflow_manifest.steps
        for output in step.outputs
    )


def _planning_rows() -> list[dict[str, str]]:
    return [
        {
            "kind": "spec",
            "path": f"docs/superpowers/specs/{TASK_SLUG}-spec.md",
            "content": (
                "---\nstatus: accepted\n---\n# Spec\n\n"
                "## Requirements\n\nPlanning artifacts stay manifest-bound.\n"
            ),
        },
        {
            "kind": "design",
            "path": f"docs/superpowers/specs/{TASK_SLUG}-design.md",
            "content": (
                "---\nstatus: accepted\n---\n# Design\n\n"
                "## Decisions\n\nBind each planning kind to its declared path.\n"
            ),
        },
        {
            "kind": "plan",
            "path": f"docs/superpowers/plans/{TASK_SLUG}.md",
            "content": (
                "---\nstatus: accepted\n---\n# Plan\n\n"
                "## Task 1\n\nImplement the binding.\n"
            ),
        },
    ]


def test_fix_standard_manifest_publishes_spec_design_and_plan(tmp_path: Path) -> None:
    """A fix-standard planning run must bind and publish all three artifact kinds."""

    rows = _planning_rows()
    rollback = manager._publish_planning_artifacts(
        str(tmp_path),
        rows,
        work_id=CHANGE,
        allowed_refs=_fix_standard_manifest_outputs(),
    )

    for row in rows:
        target = tmp_path / row["path"]
        assert target.read_text(encoding="utf-8") == row["content"]
    rollback()


def test_fix_standard_manifest_rejects_an_unbound_planning_path(tmp_path: Path) -> None:
    row = {
        "kind": "spec",
        "path": "docs/superpowers/specs/not-this-work-spec.md",
        "content": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nNo.\n",
    }

    with pytest.raises(ValueError, match="outside governed roots"):
        manager._publish_planning_artifacts(
            str(tmp_path),
            [row],
            work_id=CHANGE,
            allowed_refs=_fix_standard_manifest_outputs(),
        )
    assert not (tmp_path / row["path"]).exists()


def _content_needs_human_candidate() -> ClaimCandidate:
    authority = WorkAuthority._verified(
        repo="acme/demo",
        work_id=TASK_SLUG,
        mapped_issues=(802,),
        confirmed_todo=True,
        auto_label=False,
        source_revisions=("issue:802@open",),
        provider_revision="gh-1",
        last_success_epoch=1,
        snapshot_hash="a" * 64,
    )
    return ClaimCandidate(
        authority=authority,
        repo=authority.repo,
        work_id=authority.work_id,
        source_revisions=authority.source_revisions,
        confirmed_todo=authority.confirmed_todo,
        confirmed_issue=802,
        auto_label=False,
        active_run_id="workflow-" + "a" * 20,
        active_claim_key="claim:v1:" + "b" * 64,
        active_status="needs_human",
        active_snapshot_hash=authority.snapshot_hash,
        active_source_revisions=authority.source_revisions,
        active_provider_revision=authority.github_provider_revision,
        active_authority_digest="c" * 64,
        active_phase="define",
        active_planning_failure_classification="content",
        active_planning_failure_reason="accepted planning content was rejected",
    )


def test_content_needs_human_exposes_hint_and_abandon() -> None:
    decision = _resume_decision(_content_needs_human_candidate())

    assert decision.action == "needs_human"
    assert decision.next_actions == ("abandon",)
    assert isinstance(decision.next_step_hint, str)
    assert "abandon" in decision.next_step_hint


def test_content_needs_human_status_exposes_hint_and_abandon(tmp_path: Path) -> None:
    run_id = "workflow-" + "d" * 20
    evidence = tmp_path / "evidence" / "planning-recovery" / "failure.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        json.dumps(
            {
                "schema": "cortex-planning-failure/v1",
                "run_id": run_id,
                "classification": "content",
                "reason": "primary-artifact-write-rejected",
            }
        ),
        encoding="utf-8",
    )
    run = SimpleNamespace(
        run_id=run_id,
        work_id=TASK_SLUG,
        repo="acme/demo",
        current_phase="define",
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason={
            "schema_version": 1,
            "reason": "brainstorm-not-ready",
            "detail": "content planning output was rejected",
            "source": "manager.apply_workflow_action:start-brainstorm",
        },
        evidence_refs=(str(evidence),),
        updated_at="2026-08-27T00:00:00+00:00",
        workspace_root=str(tmp_path),
        frozen_readiness=None,
    )

    entry = manager.workflow_status_entry(None, run)

    assert entry["next_actions"] == ["abandon"]
    assert isinstance(entry["next_step_hint"], str)
    assert "abandon" in entry["next_step_hint"]
