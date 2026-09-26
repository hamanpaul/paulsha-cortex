from __future__ import annotations

import pytest

from paulsha_cortex.coordinator import manager, work_actions
from paulsha_cortex.coordinator.diagnostics import diagnostic_reason
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.claim import WorkAuthority
from paulsha_cortex.monitor.correlation import CorrelatedWork, CorrelationResult
from paulsha_cortex.monitor.lifecycle import project_work_items
from paulsha_cortex.monitor.providers import WorkflowRegistryProvider
from paulsha_cortex.monitor.work_api import _merge_observations

from test_review_gate_adjudication_exit import _review_fixture
from test_workflow_production_wiring import _manifest


def _seed_pre_candidate_recovery(tmp_path):
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    owner = {"repo": "acme/demo", "work_id": "work", "slice_id": "work-slice"}
    attempt_id = "attempt-work"
    builder = registry.create_job(
        task="work-slice",
        persona="builder",
        branch="feature/work",
        pane="",
        worktree=str(tmp_path / "builder-worktree"),
        owner_identity=owner,
        attempt_id=attempt_id,
    )
    registry.update_headless_result(builder["job_id"], status="failed", exit_code=1)
    registry.create_slice(
        slice_id="work-slice",
        spec_path="specs/work.md",
        spec_hash="a" * 64,
        plan_path="plans/work.md",
        plan_hash="b" * 64,
        target_branch="main",
        builder_job_id=builder["job_id"],
        candidate=None,
        owner_identity=owner,
        attempt_id=attempt_id,
    )
    registry.update_slice("work-slice", state="needs_human", gate_state="needs_human")
    authority = WorkAuthority._verified(
        repo="acme/demo",
        work_id="work",
        mapped_issues=(),
        mapped_prs=(),
        mapped_openspec=(),
        mapped_todo_paths=(),
        confirmed_todo=False,
        auto_label=False,
        source_revisions=("source-revision",),
        provider_revision="provider-revision",
        last_success_epoch=1,
        snapshot_hash="3" * 64,
        requires_github_authority=False,
    )
    run = registry._manager_create_workflow_run(
        work_id="work",
        repo="acme/demo",
        claim_key=work_actions._expected_claim_key(authority),
        source_revision=work_actions.work_authority_digest(authority),
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=_manifest().steps,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=diagnostic_reason(
            "builder-terminal-failure",
            "builder stopped before producing a candidate",
            source="tests.test_recovery_action_exposure_546",
        ),
    )
    return registry, run, authority


def test_claim_decision_includes_admitted_job_recovery_action(tmp_path):
    registry, run, authority = _seed_pre_candidate_recovery(tmp_path)

    response = work_actions._claim_action(
        args={"action": "start", "repo": run.repo, "work_id": run.work_id},
        authority=authority,
        requested_by="operator",
        now_epoch=1,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    assert response["action"] == "needs_human"
    assert response["run"]["run_id"] == run.run_id
    assert set(response["next_actions"]) == {"abandon", "recover-pre-candidate"}


def test_status_projects_recover_pre_candidate_when_owner_admission_allows_it(tmp_path):
    registry, run, _authority = _seed_pre_candidate_recovery(tmp_path)

    entry = manager.workflow_status_entry(registry, run)

    assert set(entry["next_actions"]) == {"abandon", "recover-pre-candidate"}
    assert "recover-pre-candidate" in manager.allowed_slice_actions(
        registry, registry.get_slice("work-slice")
    )


def test_recovery_projection_hides_unbound_pre_candidate_action(tmp_path):
    registry, run, _authority = _seed_pre_candidate_recovery(tmp_path)
    registry._slices[0]["builder_job_id"] = None

    with pytest.raises(RuntimeError, match="owner-bound builder job"):
        manager._pre_candidate_recovery_admission(
            registry,
            registry.get_slice("work-slice"),
            expected_owner={"repo": run.repo, "work_id": run.work_id},
        )

    assert "recover-pre-candidate" not in work_actions._phase_recovery_actions(
        run, registry
    )


def test_work_list_projects_admitted_workflow_recovery_actions(tmp_path):
    registry, run, _authority = _seed_pre_candidate_recovery(tmp_path)
    provider = WorkflowRegistryProvider(run.repo, state_path=registry._state_path)

    snapshot = provider.scan()

    assert snapshot.status == "ok"
    observations = _merge_observations((snapshot,))
    recovery_by_work = observations["workflow_next_actions"]
    row = recovery_by_work[run.work_id]
    assert row["run_id"] == run.run_id
    assert set(row["actions"]) == {"abandon", "recover-pre-candidate"}
    source = snapshot.sources[0]
    correlation = CorrelationResult(
        groups=(
            CorrelatedWork(
                work_id=run.work_id,
                title="Work",
                sources=(source,),
                confidence="confirmed",
            ),
        ),
        source_owners={source.source_id: run.work_id},
        exclusions=(),
        explanations={},
    )

    projection = project_work_items(
        correlation,
        repo=run.repo,
        updated_at="2026-09-26T00:00:00Z",
        workflow_next_actions_by_work=recovery_by_work,
    )

    assert set(projection.items[0].next_actions) == {
        "abandon",
        "recover-pre-candidate",
    }


def test_job_derived_retry_build_is_projected_across_claim_list_and_status(tmp_path):
    _source_snapshot, authority, registry, run = _review_fixture(tmp_path)
    assert "retry-build" in work_actions._phase_recovery_actions(run, registry)

    claim = work_actions._claim_action(
        args={"action": "start", "repo": run.repo, "work_id": run.work_id},
        authority=authority,
        requested_by="operator",
        now_epoch=1,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )
    status = manager.workflow_status_entry(registry, run)
    assert "retry-build" in claim["next_actions"]
    assert "retry-build" in status["next_actions"]

    provider = WorkflowRegistryProvider(run.repo, state_path=registry._state_path)
    provider_snapshot = provider.scan()
    assert provider_snapshot.status == "ok"
    observations = _merge_observations((provider_snapshot,))
    source = provider_snapshot.sources[0]
    correlation = CorrelationResult(
        groups=(
            CorrelatedWork(
                work_id=run.work_id,
                title="Work",
                sources=(source,),
                confidence="confirmed",
            ),
        ),
        source_owners={source.source_id: run.work_id},
        exclusions=(),
        explanations={},
    )
    projection = project_work_items(
        correlation,
        repo=run.repo,
        updated_at="2026-09-26T00:00:00Z",
        workflow_next_actions_by_work=observations["workflow_next_actions"],
    )
    assert "retry-build" in projection.items[0].next_actions
