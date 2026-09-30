"""#1220：GitHub 未建立 PR closing reference 時提供結構化人工出口。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_post_merge_run_resolution_1141 as ship_fixture
from paulsha_cortex.coordinator import manager, work_actions
from paulsha_cortex.coordinator.github_delivery import GateResult


def _ship_with_remote_facts(lane, *, closing_issues=(), review_threads=()):
    original_fetch = lane.github.fetch_delivery_facts

    def fetch_delivery_facts(**kwargs):
        facts = original_fetch(**kwargs)
        return replace(
            facts,
            closing_issues=tuple(closing_issues),
            review_threads=tuple(review_threads),
        )

    lane.github.fetch_delivery_facts = fetch_delivery_facts
    ship_fixture._open_pr_snapshot(lane.snapshot)
    return lane.ship(now=2000.0)


def test_missing_closing_reference_becomes_structured_needs_human(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)

    result = _ship_with_remote_facts(lane)

    assert result["action"] == "needs_human"
    assert result["reason"] == "closing-reference-missing"
    run = lane.registry.get_workflow_run(lane.run_id)
    reason = run.needs_human_reason
    assert reason["reason"] == "closing-reference-missing"
    assert reason["context"]["pr"] == "8"
    assert reason["context"]["missing_issues"] == "12"
    assert reason["context"]["head"] == ship_fixture.HEAD


def test_missing_closing_reference_with_another_gate_reason_still_raises(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    original_evaluate = work_actions.evaluate_delivery_gate

    def evaluate_with_open_thread(**kwargs):
        result = original_evaluate(**kwargs)
        if "closing-issue-missing" in result.reasons:
            return GateResult(
                allowed=False,
                reasons=(*result.reasons, "review-thread-open"),
            )
        return result

    monkeypatch.setattr(work_actions, "evaluate_delivery_gate", evaluate_with_open_thread)

    with pytest.raises(RuntimeError, match="closing-issue-missing, review-thread-open"):
        _ship_with_remote_facts(lane)


def test_maintainer_review_path_uses_the_same_structured_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    lane.ship(now=2000.0)
    state = work_actions._load_runs(lane.journal)
    active = state["runs"][lane.run_id]
    authority = work_actions.load_work_authority(
        repo=ship_fixture.REPO,
        work_id=ship_fixture.WORK_ID,
        snapshot_path=lane.snapshot,
    )
    review_path = tmp_path / "maintainer-review.json"
    review_path.write_text("{}", encoding="utf-8")
    foreign_path = tmp_path / "foreign-review.json"
    foreign_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(work_actions, "_validate_maintainer_review", lambda **kwargs: {})
    monkeypatch.setattr(work_actions, "_absolute_file", lambda value, **kwargs: Path(value))
    remote = replace(
        lane.github.fetch_delivery_facts(),
        closing_issues=(),
    )

    result = work_actions._ship_with_maintainer_review(
        args={
            "maintainer_review_path": str(review_path),
            "maintainer_review_hash": "a" * 64,
            "foreign_review_path": str(foreign_path),
        },
        active=active,
        state=state,
        state_path=lane.journal,
        authority=authority,
        canonical_run=lane.registry.get_workflow_run(lane.run_id),
        workflow_registry=lane.registry,
        binding=active["delivery_binding"],
        preflight=SimpleNamespace(head=ship_fixture.HEAD, tree_hash=ship_fixture.TREE),
        remote=remote,
        orchestrator=SimpleNamespace(),
        github=lane.github,
        ship=active["ship"],
        fix_rounds=0,
    )

    assert result["action"] == "needs_human"
    assert result["reason"] == "closing-reference-missing"
    reason = lane.registry.get_workflow_run(lane.run_id).needs_human_reason
    assert reason["context"]["pr"] == "8"


def test_closing_reference_projection_offers_resume_and_operator_hint(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    _ship_with_remote_facts(lane)
    run = lane.registry.get_workflow_run(lane.run_id)

    assert "resume" in work_actions._phase_recovery_actions(run, lane.registry)
    entry = manager.workflow_status_entry(lane.registry, run)

    assert "resume" in entry["next_actions"]
    hint = entry["next_step_hint"]
    assert "Development" in hint
    assert f"cortex work resume {ship_fixture.WORK_ID} --repo {ship_fixture.REPO}" in hint
    assert "cortex work retire-delivered" in hint
