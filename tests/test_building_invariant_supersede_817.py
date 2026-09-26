from __future__ import annotations

from types import SimpleNamespace

import pytest

from paulsha_cortex.control import contract
from paulsha_cortex.coordinator import cli, manager, manager_daemon
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.porcelain import recover


def _create_slice(registry: JobRegistry, tmp_path, *, state: str = "pending") -> dict:
    row = registry.create_slice(
        slice_id="slice-a",
        spec_path=str(tmp_path / "specs" / "slice-a.md"),
        spec_hash="spec-hash",
        plan_path=str(tmp_path / "plans" / "slice-a.md"),
        plan_hash="plan-hash",
        target_branch="main",
    )
    if state != "pending":
        row = registry.update_slice(
            row["slice_id"],
            state=state,
            gate_state="needs_human" if state == "needs_human" else "pending",
        )
    return row


def _dispatcher(registry: JobRegistry):
    return SimpleNamespace(
        _registry=registry,
        _git_runner=None,
        poll_headless_done=lambda job_id: registry.get_job(job_id),
    )


def test_complete_tick_reconciles_building_without_current_job(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "runtime" / "jobs.json")
    slice_row = _create_slice(registry, tmp_path)
    registry.update_slice(slice_row["slice_id"], state="building")

    manager.complete_tick(_dispatcher(registry), handoff_dir=str(tmp_path / "handoff"))

    updated = registry.get_slice(slice_row["slice_id"])
    assert updated["state"] == "needs_human"
    assert updated["gate_state"] == "needs_human"
    action = updated["actions"][-1]
    assert action["action"] == "manager-reconcile-building-without-inflight-job"
    assert action["diagnostic_reason"]["reason"] == "building-without-inflight-job"


def test_complete_tick_preserves_building_with_current_builder_job(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "runtime" / "jobs.json")
    job = registry.create_job(
        task="slice-a",
        persona="builder",
        branch="feature/slice-a",
        pane="pane-a",
        worktree=str(tmp_path / "worktrees" / "slice-a"),
    )
    registry.create_slice(
        slice_id="slice-a",
        spec_path=str(tmp_path / "specs" / "slice-a.md"),
        spec_hash="spec-hash",
        plan_path=str(tmp_path / "plans" / "slice-a.md"),
        plan_hash="plan-hash",
        target_branch="main",
        builder_job_id=job["job_id"],
    )
    registry.update_slice("slice-a", state="building")

    manager.complete_tick(_dispatcher(registry), handoff_dir=str(tmp_path / "handoff"))

    assert registry.get_slice("slice-a")["state"] == "building"


def test_runtime_status_reconciles_stale_building_and_exposes_supersede(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "runtime" / "jobs.json")
    slice_row = _create_slice(registry, tmp_path)
    registry.update_slice(slice_row["slice_id"], state="building")
    provider = manager_daemon.build_runtime_status_provider(
        registry=registry,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        scan_specs_fn=lambda _path: [],
        ready_units_fn=lambda _metas, _predicate: [],
        recent_done_window_seconds=None,
    )

    snapshot = provider()

    assert registry.get_slice(slice_row["slice_id"])["state"] == "needs_human"
    assert len(snapshot["attention"]) == 1
    entry = snapshot["attention"][0]
    assert entry["diagnostic_reason"]["reason"] == "building-without-inflight-job"
    assert "building-without-inflight-job" in entry["reason"]
    assert "supersede" in entry["next_actions"]


def test_supersede_records_actor_reason_and_exact_binding_revision(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "runtime" / "jobs.json")
    slice_row = _create_slice(registry, tmp_path, state="needs_human")

    result = manager.apply_slice_action(
        _dispatcher(registry),
        slice_id=slice_row["slice_id"],
        action="supersede",
        actor="operator",
        reason="後續 retry 已接手此分析",
        expected_binding_revision=slice_row["binding_revision"],
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
    )

    updated = registry.get_slice(slice_row["slice_id"])
    assert result["action"] == "supersede"
    assert updated["state"] == "superseded"
    action = updated["actions"][-1]
    assert action["action"] == "operator-supersede"
    assert action["actor"] == "operator"
    assert action["reason"] == "後續 retry 已接手此分析"
    assert action["expected_binding_revision"] == slice_row["binding_revision"]
    assert action["result"] == "ok"

    replay = manager.apply_slice_action(
        _dispatcher(registry),
        slice_id=slice_row["slice_id"],
        action="supersede",
        actor="operator",
        reason="後續 retry 已接手此分析",
        expected_binding_revision=slice_row["binding_revision"],
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
    )

    assert replay["result"] == "already-superseded"
    assert len(registry.get_slice(slice_row["slice_id"])["actions"]) == 1


def test_supersede_rejects_stale_binding_revision_without_mutation(tmp_path) -> None:
    registry = JobRegistry(state_path=tmp_path / "runtime" / "jobs.json")
    slice_row = _create_slice(registry, tmp_path, state="needs_human")
    expected_revision = slice_row["binding_revision"]
    registry.update_slice(slice_row["slice_id"], state="failed", gate_state="failed")
    actions_before = registry.get_slice(slice_row["slice_id"])["actions"]

    with pytest.raises(ValueError, match="expected_binding_revision"):
        manager.apply_slice_action(
            _dispatcher(registry),
            slice_id=slice_row["slice_id"],
            action="supersede",
            actor="operator",
            reason="後續 retry 已接手此分析",
            expected_binding_revision=expected_revision,
            specs_dir=str(tmp_path / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
        )

    updated = registry.get_slice(slice_row["slice_id"])
    assert updated["state"] == "failed"
    assert updated["actions"] == actions_before


def test_slice_action_contract_requires_supersede_audit_and_cas() -> None:
    request = contract.build_request(
        req_type="slice-action",
        args={
            "slice_id": "slice-a",
            "action": "supersede",
            "actor": "operator",
            "reason": "後續 retry 已接手此分析",
            "expected_binding_revision": 3,
        },
        requested_by="operator",
    )

    assert contract.validate_request(request)["args"]["expected_binding_revision"] == 3
    request["args"].pop("reason")
    with pytest.raises(ValueError, match="reason"):
        contract.validate_request(request)


def test_slice_action_cli_forwards_supersede_audit_and_cas() -> None:
    submitted = []
    result = cli.main(
        [
            "slice-action",
            "slice-a",
            "supersede",
            "--actor",
            "operator",
            "--reason",
            "後續 retry 已接手此分析",
            "--expected-binding-revision",
            "3",
        ],
        control_read_status=lambda: {"degraded": False},
        control_submit_request=lambda kind, args, actor: submitted.append((kind, args, actor))
        or "req-slice-1",
        control_poll_done=lambda *_args, **_kwargs: {"status": "ok", "result": {}},
    )

    assert result == 0
    assert submitted[0][0] == "slice-action"
    assert submitted[0][1]["reason"] == "後續 retry 已接手此分析"
    assert submitted[0][1]["expected_binding_revision"] == 3


def test_recover_slice_cli_forwards_supersede_audit_and_cas() -> None:
    args = recover._build_parser().parse_args(
        [
            "slice",
            "slice-a",
            "supersede",
            "--actor",
            "operator",
            "--reason",
            "後續 retry 已接手此分析",
            "--expected-binding-revision",
            "3",
        ]
    )

    assert recover._slice_args(args) == {
        "slice_id": "slice-a",
        "action": "supersede",
        "actor": "operator",
        "expected_binding_revision": 3,
        "reason": "後續 retry 已接手此分析",
    }
