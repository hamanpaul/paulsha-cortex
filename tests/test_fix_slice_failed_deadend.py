from __future__ import annotations

from pathlib import Path

from paulsha_cortex.coordinator import manager, manager_daemon
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.registry import JobRegistry


def _create_failed_slice(state_path: Path, *, slice_id: str = "slice-a") -> JobRegistry:
    reg = JobRegistry(state_path=state_path)
    builder_job = reg.create_job(
        task=slice_id,
        persona="builder",
        branch=f"feature/{slice_id}",
        pane="",
        worktree=f"/wt/{slice_id}",
    )
    reg.create_slice(
        slice_id=slice_id,
        spec_path=f"specs/{slice_id}.md",
        spec_hash="spec-sha",
        plan_path=f"plans/{slice_id}.md",
        plan_hash="plan-sha",
        target_branch="main",
        builder_job_id=builder_job["job_id"],
        reviewer_job_id=None,
        candidate=None,
    )
    reg.update_slice(slice_id, state="failed", gate_state="failed")
    return reg


def _failed_retry_scan_specs() -> list[dict[str, object]]:
    return [
        {
            "slice_id": "slice-a",
            "dispatch": "auto",
            "plan": "plans/slice-a.md",
            "depends_on": [],
        }
    ]


def _stub_dispatch_ready_factory(registry: JobRegistry):
    def _dispatch_ready(
        metas: list[dict[str, object]],
        _is_satisfied,
        _dispatcher,
        **_kwargs,
    ) -> list[dict[str, object]]:
        registry.update_slice("slice-a", state="needs_human", gate_state="needs_human")
        registry.record_action(
            "slice-a",
            action="operator-retry-build",
            actor="operator",
            result="ok",
        )
        return [{"slice_id": "slice-a", "job_id": "slice-a-2"}]

    return _dispatch_ready


def test_failed_slice_retry_build_is_allowed_and_leaves_recovery_action(tmp_path):
    state_path = tmp_path / "runtime" / "coordinator" / "jobs.json"
    registry = _create_failed_slice(state_path)
    dispatcher = Dispatcher(registry, pane_sender=None, worktree_creator=None)

    result = manager.apply_slice_action(
        dispatcher,
        slice_id="slice-a",
        action="retry-build",
        actor="operator",
        specs_dir="specs",
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        scan_specs_fn=lambda _specs_dir: _failed_retry_scan_specs(),
        dispatch_ready_fn=_stub_dispatch_ready_factory(registry),
    )

    restored = registry.get_slice("slice-a")
    assert result["action"] == "retry-build"
    assert restored["state"] in {"needs_human", "building"}
    assert restored["actions"]
    assert restored["actions"][-1]["action"] in {"operator-retry-build", "operator-reset"}


def test_failed_slice_retry_build_can_transition_to_recoverable_state(tmp_path):
    state_path = tmp_path / "runtime" / "coordinator" / "jobs.json"
    registry = _create_failed_slice(state_path)
    dispatcher = Dispatcher(registry, pane_sender=None, worktree_creator=None)

    manager.apply_slice_action(
        dispatcher,
        slice_id="slice-a",
        action="retry-build",
        actor="operator",
        specs_dir="specs",
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        scan_specs_fn=lambda _specs_dir: _failed_retry_scan_specs(),
        dispatch_ready_fn=_stub_dispatch_ready_factory(registry),
    )

    restored = registry.get_slice("slice-a")
    assert restored["state"] in {"needs_human", "building"}


def test_registry_daemon_runtime_provider_reflects_external_jobs_json_updates(tmp_path):
    state_path = tmp_path / "runtime" / "coordinator" / "jobs.json"
    registry = JobRegistry(state_path=state_path)
    status_provider = manager_daemon.build_runtime_status_provider(
        registry=registry,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        scan_specs_fn=lambda _specs_dir: [],
    )

    assert status_provider()["slices"] == []

    external_registry = JobRegistry(state_path=state_path)
    external_registry.create_slice(
        slice_id="slice-a",
        spec_path="specs/slice-a.md",
        spec_hash="spec-sha",
        plan_path="plans/slice-a.md",
        plan_hash="plan-sha",
        target_branch="main",
        builder_job_id=None,
        reviewer_job_id=None,
        candidate=None,
    )

    status = status_provider()
    assert {entry["slice_id"] for entry in status["slices"]} == {"slice-a"}
