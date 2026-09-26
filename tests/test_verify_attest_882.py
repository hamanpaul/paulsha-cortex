from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.registry import JobRegistry
from diagnostic_fixtures import fixture_needs_human_reason


HEAD = "a" * 40


def _snapshot(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "remote", "add", "origin", "git@github.com:acme/demo.git"],
        check=True,
    )
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "mapped_issues": [12],
                        "mapped_prs": [],
                        "mapped_openspec": [],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": False,
                        "source_revisions": ["issue:12@open", "path:docs/todo.md@1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _verify_run(tmp_path: Path, *, phase: str = "verify"):
    snapshot = _snapshot(tmp_path)
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    run_id = started["result"]["run"]["run_id"]
    run = registry.get_workflow_run(run_id)
    phases = ("claim", "define", "plan", "build", "verify", "review", "ship")
    current_index = phases.index(run.current_phase)
    target_index = phases.index(phase)
    while current_index < target_index:
        current_index += 1
        run = registry._manager_update_workflow_run(
            run_id, current_phase=phases[current_index]
        )
    steps = tuple(
        replace(
            step,
            gate_result=(
                "passed"
                if phases.index(step.phase) < phases.index("verify")
                else "needs_human"
                if step.phase == "verify"
                else step.gate_result
            ),
        )
        for step in run.steps
    )
    run = registry._manager_update_workflow_run(
        run_id,
        candidate_head=HEAD,
        steps=steps,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=fixture_needs_human_reason(),
    )
    return snapshot, state, registry, run


def _attest(
    *,
    snapshot: Path,
    state: Path,
    registry: JobRegistry,
    run,
    payload: dict,
    expected_candidate: str = HEAD,
):
    return work_actions.execute_work_action(
        args={
            "action": "verify-attest",
            "repo": "acme/demo",
            "work_id": "demo",
            "actor": "operator",
            "expected_candidate": expected_candidate,
            **payload,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 210,
        workflow_registry=registry,
    )


def test_verify_attest_records_immutable_full_suite_and_advances_exact_candidate(
    tmp_path: Path,
) -> None:
    snapshot, state, registry, run = _verify_run(tmp_path)

    result = _attest(
        snapshot=snapshot,
        state=state,
        registry=registry,
        run=run,
        payload={
            "full_suite_command": "python -m pytest -q",
            "result_summary": {"passed": 5987, "failed": 0},
        },
    )

    assert result["result"]["action"] == "verify-attested"
    evidence_path = Path(result["result"]["ref"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence_path.stat().st_mode & 0o222 == 0
    assert evidence["candidate"] == HEAD
    assert evidence["full_suite_command"] == "python -m pytest -q"
    assert evidence["result_summary"] == {"passed": 5987, "failed": 0}
    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.current_phase == "review"
    assert persisted.verified_head == HEAD
    assert persisted.candidate_head == HEAD
    assert "needs_human" not in persisted.facets
    assert str(evidence_path) in persisted.evidence_refs
    assert all(
        step.gate_result == "passed"
        for step in persisted.steps
        if step.phase == "verify"
    )


def test_verify_attest_rejects_candidate_mismatch_without_evidence(tmp_path: Path) -> None:
    snapshot, state, registry, run = _verify_run(tmp_path)

    with pytest.raises(RuntimeError, match="Candidate CAS mismatch"):
        _attest(
            snapshot=snapshot,
            state=state,
            registry=registry,
            run=run,
            payload={
                "full_suite_command": "python -m pytest -q",
                "result_summary": {"passed": 10, "failed": 0},
            },
            expected_candidate="b" * 40,
        )

    assert not (state.parent / "evidence" / "verify-attest").exists()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"result_summary": {"passed": 10, "failed": 0}}, "payload invalid"),
        (
            {
                "full_suite_command": "python -m pytest -q",
                "result_summary": {"passed": 9, "failed": 1},
            },
            "payload invalid",
        ),
    ],
)
def test_verify_attest_requires_full_suite_command_and_zero_failures(
    tmp_path: Path, payload: dict, message: str
) -> None:
    snapshot, state, registry, run = _verify_run(tmp_path)

    with pytest.raises(ValueError, match=message):
        _attest(
            snapshot=snapshot,
            state=state,
            registry=registry,
            run=run,
            payload=payload,
        )

    assert not (state.parent / "evidence" / "verify-attest").exists()


def test_verify_attest_rejects_non_verify_phase(tmp_path: Path) -> None:
    snapshot, state, registry, run = _verify_run(tmp_path, phase="review")

    with pytest.raises(RuntimeError, match="verify-phase workflow"):
        _attest(
            snapshot=snapshot,
            state=state,
            registry=registry,
            run=run,
            payload={
                "full_suite_command": "python -m pytest -q",
                "result_summary": {"passed": 10, "failed": 0},
            },
        )

    assert not (state.parent / "evidence" / "verify-attest").exists()


def test_verify_attest_rejects_active_verification_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot, state, registry, run = _verify_run(tmp_path)
    monkeypatch.setattr(
        registry,
        "list_jobs",
        lambda: [{"workflow_run_id": run.run_id, "workflow_phase": "verify", "status": "running"}],
    )

    with pytest.raises(RuntimeError, match="active workflow job"):
        _attest(
            snapshot=snapshot,
            state=state,
            registry=registry,
            run=run,
            payload={
                "full_suite_command": "python -m pytest -q",
                "result_summary": {"passed": 10, "failed": 0},
            },
        )

    assert not (state.parent / "evidence" / "verify-attest").exists()


def test_registry_verify_attest_transition_rechecks_candidate_cas(tmp_path: Path) -> None:
    _snapshot_path, _state, registry, run = _verify_run(tmp_path)

    with pytest.raises(ValueError, match="Candidate CAS mismatch"):
        registry._manager_advance_verify_attest(
            run.run_id,
            expected_candidate="b" * 40,
            evidence_ref="evidence/verify-attest.json",
        )

    assert registry.get_workflow_run(run.run_id).current_phase == "verify"
