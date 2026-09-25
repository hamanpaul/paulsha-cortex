from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import job_workspace, manager, work_actions, work_bridge
from paulsha_cortex.coordinator.claim import load_work_authority, work_authority_digest
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import (
    DEFAULT_CARDS_PATH,
    DEFAULT_COMBOS_DIR,
    load_cards,
    load_combo,
)

from diagnostic_fixtures import fixture_needs_human_reason
from test_preflight_closeout_order import (  # noqa: E402
    FakeGitHubDeliveryClient,
    RunnerResult,
    ShipHarness,
    SpyRunner,
    _capture,
    _repo,
    _ship_harness,
    _snapshot,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
REPO = "acme/demo"
WORK_ID = "work"
TASK_SLUG = "fix-standard-archive-step-evidence-885"


def _git(repo: Path, *args: str, input: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        input=input,
    ).stdout.strip()


def _combo_manifest_steps(combo_id: str) -> tuple[WorkflowStep, ...]:
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / f"{combo_id}.yaml", cards)
    result = compile_combo(
        combo,
        cards,
        TASK_SLUG,
        change=WORK_ID,
        allow_external=True,
        repo_root=REPO_ROOT,
    )
    assert result.workflow_manifest is not None
    return result.workflow_manifest.steps


def _fix_standard_manifest_steps() -> tuple[WorkflowStep, ...]:
    return _combo_manifest_steps("fix-standard")


def _review_complete_fix_standard_steps() -> tuple[WorkflowStep, ...]:
    return tuple(
        replace(step, gate_result="passed") if step.phase != "ship" else step
        for step in _fix_standard_manifest_steps()
    )


def _review_complete_combo_steps(combo_id: str) -> tuple[WorkflowStep, ...]:
    return tuple(
        replace(step, gate_result="passed") if step.phase != "ship" else step
        for step in _combo_manifest_steps(combo_id)
    )


def _snapshot_without_openspec(path: Path) -> Path:
    _snapshot(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    item = payload["work_items"][0]
    item["mapped_openspec"] = []
    item["source_revisions"] = [
        revision
        for revision in item["source_revisions"]
        if not revision.startswith("openspec:")
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _archive_job(
    *,
    run,
    subject_head: str,
    executor: str = "cortex-manager",
    model_id: str = "deterministic",
    independence_domain: str = "cortex",
    persona: str = "manager",
    evidence: bool = True,
) -> dict[str, object]:
    job = {
        "job_id": f"archive-{subject_head[:8]}",
        "workflow_run_id": run.run_id,
        "workflow_phase": "ship",
        "workflow_card": "openspec-archive",
        "persona": persona,
        "executor": executor,
        "model_id": model_id,
        "independence_domain": independence_domain,
        "status": "exited",
        "exit_code": 0,
        "subject_head": subject_head,
    }
    if evidence:
        job["workflow_evidence"] = {
            "kind": "ship",
            "path": "evidence/workflow/archive.json",
            "hash": "f" * 64,
        }
    return job


def _create_fix_standard_run(
    *,
    registry: JobRegistry,
    repo_root: Path,
    snapshot: Path,
    candidate: str,
    combo: str = "fix-standard",
    steps: tuple[WorkflowStep, ...] | None = None,
    current_phase: str = "review",
    verified_head: str | None = None,
    attempts: dict[str, int] | None = None,
    facets: tuple[str, ...] = (),
    gate_status: str = "running",
):
    authority = load_work_authority(repo=REPO, work_id=WORK_ID, snapshot_path=snapshot)
    return registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision=work_authority_digest(authority),
        workspace_root=str(repo_root),
        combo=combo,
        current_phase=current_phase,
        steps=steps or _review_complete_combo_steps(combo),
        issue_refs=(f"{REPO}#14",),
        openspec_refs=authority.mapped_openspec,
        pr_refs=(),
        attempts=attempts or {"build": 1, "verify": 1, "review": 1},
        candidate_head=candidate,
        verified_head=verified_head if verified_head is not None else candidate,
        facets=facets,
        gate_status=gate_status,
        needs_human_reason=(
            fixture_needs_human_reason() if "needs_human" in facets else None
        ),
    )


def _record_manager_ship_job(
    *,
    registry: JobRegistry,
    state_root: Path,
    run,
    repo: Path,
    card: str,
    subject_head: str,
) -> None:
    work_bridge._record_manager_ship_job(
        registry=registry,
        state_root=state_root,
        run=run,
        worktree=repo,
        branch="feature/14-work",
        card=card,
        old_head=subject_head,
        new_head=subject_head,
    )


def _coexisting_candidate_commit(repo: Path, *, archive_entry: str = "2026-09-14-work") -> str:
    archive = repo / "openspec" / "changes" / "archive" / archive_entry
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "proposal.md").write_text("# Archived proposal\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add archived change"], check=True)
    return _git(repo, "rev-parse", "HEAD")


def _repair_action_text(run) -> str:
    return str(next(step.action for step in run.steps if step.card == "subagent-build"))


def _use_fix_standard_ship_steps(harness: ShipHarness):
    run = harness.registry.get_workflow_run(harness.run_id)
    return harness.registry._manager_update_workflow_run(
        run.run_id,
        steps=_review_complete_fix_standard_steps(),
        verified_head=harness.candidate,
    )


def _swap_ship_runner(harness: ShipHarness, runner: SpyRunner) -> None:
    harness.runner = runner
    harness.validator = work_bridge.build_production_ship_validator(
        registry=harness.registry,
        coordinator_root=harness.state_root,
        snapshot_path=harness.snapshot,
        runner=runner,
    )


class _ArchiveCommandRunner(SpyRunner):
    def __init__(
        self,
        *,
        repo: Path,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        move_archive: bool,
        lingering_active_change: str | None = None,
    ) -> None:
        super().__init__(repo=repo)
        self._archive_stdout = stdout
        self._archive_stderr = stderr
        self._archive_returncode = returncode
        self._move_archive = move_archive
        self._lingering_active_change = lingering_active_change

    def __call__(self, argv, **kwargs):
        command = [str(value) for value in argv]
        if command[:2] == ["openspec", "archive"]:
            self.calls.append(command)
            if self._move_archive:
                self._apply_archive(command[-1], cwd=kwargs.get("cwd"))
                self._leave_lingering_active_change(command[-1], cwd=kwargs.get("cwd"))
            return RunnerResult(
                self._archive_returncode,
                stdout=self._archive_stdout,
                stderr=self._archive_stderr,
            )
        return super().__call__(argv, **kwargs)

    def _leave_lingering_active_change(self, change: str, *, cwd: str | None = None) -> None:
        if self._lingering_active_change is None:
            return
        root = Path(cwd) if cwd else self.repo
        active = root / "openspec" / "changes" / change
        if active.exists() or active.is_symlink():
            if active.is_dir():
                shutil.rmtree(active)
            else:
                active.unlink()
        if self._lingering_active_change == "file":
            active.write_text("# lingering active change\n", encoding="utf-8")
            return
        if self._lingering_active_change == "broken-symlink":
            active.symlink_to("missing-active-change")
            return
        raise AssertionError(
            f"unexpected lingering active change kind {self._lingering_active_change}"
        )


def test_fix_standard_archive_applied_uses_registry_ship_evidence_and_step_priority(
    tmp_path: Path,
) -> None:
    repo, archive_candidate = _repo(
        tmp_path / "archive-predicate-repo",
        active_change=False,
        archived_change=False,
    )
    (repo / "repair.txt").write_text("repair\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "repair.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "repair"], check=True)
    final_candidate = _git(repo, "rev-parse", "HEAD")
    archive_tree = _git(repo, "rev-parse", f"{archive_candidate}^{{tree}}")
    sibling_candidate = _git(
        repo,
        "commit-tree",
        archive_tree,
        "-p",
        archive_candidate,
        input="unrelated sibling\n",
    )
    run = SimpleNamespace(
        run_id="workflow-archive-predicate",
        repo=REPO,
        steps=_review_complete_fix_standard_steps(),
        candidate_head=final_candidate,
        workspace_root=str(repo),
    )

    assert manager._manager_archive_applied(
        run,
        registry=SimpleNamespace(
            list_jobs=lambda: [_archive_job(run=run, subject_head=final_candidate)]
        ),
    )
    assert work_bridge._manager_archive_applied(
        run,
        registry=SimpleNamespace(
            list_jobs=lambda: [_archive_job(run=run, subject_head=archive_candidate)]
        ),
    )
    assert not manager._manager_archive_applied(
        run,
        registry=SimpleNamespace(
            list_jobs=lambda: [_archive_job(run=run, subject_head=sibling_candidate)]
        ),
    )
    assert not manager._manager_archive_applied(
        run,
        registry=SimpleNamespace(
            list_jobs=lambda: [
                _archive_job(run=run, subject_head=final_candidate),
                _archive_job(run=run, subject_head=archive_candidate),
            ]
        ),
    )
    assert not manager._manager_archive_applied(
        run,
        registry=SimpleNamespace(
            list_jobs=lambda: [
                _archive_job(
                    run=run,
                    subject_head=final_candidate,
                    executor="codex",
                    evidence=False,
                )
            ]
        ),
    )
    assert manager._manager_archive_applied(run) is False

    declared_step_run = SimpleNamespace(
        run_id="workflow-step-priority",
        repo=REPO,
        workspace_root=str(repo),
        candidate_head=final_candidate,
        steps=(
            WorkflowStep(
                phase="ship",
                persona="manager",
                card="openspec-archive",
                executor=None,
                model=None,
                domain=None,
                inputs=(),
                outputs=(),
                gate_result="pending",
            ),
        ),
    )
    assert manager._manager_archive_applied(
        declared_step_run,
        registry=SimpleNamespace(
            list_jobs=lambda: [_archive_job(run=declared_step_run, subject_head=final_candidate)]
        ),
    ) is False


def test_fix_standard_ship_audit_accepts_archive_job_ancestor_after_retry_build(
    tmp_path: Path,
) -> None:
    repo, archive_candidate = _repo(
        tmp_path / "ship-audit-repo",
        active_change=False,
        archived_change=False,
    )
    (repo / "repair.txt").write_text("repair\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "repair.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "repair"], check=True)
    final_candidate = _git(repo, "rev-parse", "HEAD")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=final_candidate,
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="openspec-archive",
        subject_head=archive_candidate,
    )
    run = registry._manager_update_workflow_run(run.run_id, source_revision="e" * 64)
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="policy-commit",
        subject_head=final_candidate,
    )

    audited = manager._validated_ship_steps(
        registry,
        run=run,
        candidate=final_candidate,
        coordinator_root=coordinator,
    )
    assert next(step for step in audited if step.card == "policy-commit").gate_result == "passed"

    sibling_candidate = _git(
        repo,
        "commit-tree",
        _git(repo, "rev-parse", f"{archive_candidate}^{{tree}}"),
        "-p",
        archive_candidate,
        input="unrelated sibling\n",
    )
    unrelated_root = tmp_path / "unrelated"
    unrelated_registry = JobRegistry(state_path=unrelated_root / "jobs.json")
    unrelated_run = _create_fix_standard_run(
        registry=unrelated_registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=final_candidate,
    )
    _record_manager_ship_job(
        registry=unrelated_registry,
        state_root=unrelated_root,
        run=unrelated_run,
        repo=repo,
        card="openspec-archive",
        subject_head=sibling_candidate,
    )
    _record_manager_ship_job(
        registry=unrelated_registry,
        state_root=unrelated_root,
        run=unrelated_run,
        repo=repo,
        card="policy-commit",
        subject_head=final_candidate,
    )
    with pytest.raises(ValueError, match="missing or ambiguous: openspec-archive"):
        manager._validated_ship_steps(
            unrelated_registry,
            run=unrelated_run,
            candidate=final_candidate,
            coordinator_root=unrelated_root,
        )


def test_fix_standard_ship_audit_skips_archive_when_no_openspec_is_mapped_or_declared(
    tmp_path: Path,
) -> None:
    repo, candidate = _repo(
        tmp_path / "ship-audit-no-openspec-repo",
        active_change=False,
        archived_change=False,
    )
    snapshot = _snapshot_without_openspec(tmp_path / "no-openspec-snapshot.json")
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=candidate,
    )
    assert run.openspec_refs == ()
    assert not any(
        step.phase == "ship" and step.card == "openspec-archive"
        for step in run.steps
    )

    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="policy-commit",
        subject_head=candidate,
    )

    audited = manager._validated_ship_steps(
        registry,
        run=run,
        candidate=candidate,
        coordinator_root=coordinator,
    )

    assert [
        step.card for step in audited if step.phase == "ship"
    ] == ["policy-commit"]
    assert {
        job["workflow_card"]
        for job in registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
    } == {"policy-commit"}


def test_fix_standard_ship_audit_still_requires_declared_archive_without_mapping(
    tmp_path: Path,
) -> None:
    repo, candidate = _repo(
        tmp_path / "ship-audit-declared-no-mapping-repo",
        active_change=False,
        archived_change=False,
    )
    snapshot = _snapshot_without_openspec(tmp_path / "no-openspec-snapshot.json")
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=candidate,
        combo="feature-oneshot",
    )
    assert run.openspec_refs == ()
    assert any(
        step.phase == "ship" and step.card == "openspec-archive"
        for step in run.steps
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="policy-commit",
        subject_head=candidate,
    )

    with pytest.raises(ValueError, match="missing or ambiguous: openspec-archive"):
        manager._validated_ship_steps(
            registry,
            run=run,
            candidate=candidate,
            coordinator_root=coordinator,
        )


def test_fix_standard_ship_audit_reads_registry_jobs_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, archive_candidate = _repo(
        tmp_path / "ship-audit-single-scan-repo",
        active_change=False,
        archived_change=False,
    )
    (repo / "repair.txt").write_text("repair\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "repair.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "repair"], check=True)
    final_candidate = _git(repo, "rev-parse", "HEAD")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=final_candidate,
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="openspec-archive",
        subject_head=archive_candidate,
    )
    run = registry._manager_update_workflow_run(run.run_id, source_revision="e" * 64)
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="policy-commit",
        subject_head=final_candidate,
    )
    jobs = registry.list_jobs()
    list_job_calls = 0

    def counted_list_jobs():
        nonlocal list_job_calls
        list_job_calls += 1
        return jobs

    monkeypatch.setattr(registry, "list_jobs", counted_list_jobs)

    audited = manager._validated_ship_steps(
        registry,
        run=run,
        candidate=final_candidate,
        coordinator_root=coordinator,
    )

    assert next(step for step in audited if step.card == "policy-commit").gate_result == "passed"
    assert list_job_calls == 1


def test_fix_standard_ship_audit_rejects_exact_match_archive_job_with_wrong_identity(
    tmp_path: Path,
) -> None:
    repo, candidate = _repo(
        tmp_path / "ship-audit-identity-repo",
        active_change=False,
        archived_change=False,
    )
    snapshot = _snapshot(tmp_path / "snapshot.json")
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=candidate,
    )
    bad_job = registry.create_job(
        task="wf-bad-openspec-archive",
        persona="manager",
        kind="build",
        branch="feature/work",
        pane="",
        worktree=str(repo),
        dispatch_head=candidate,
        executor="codex",
        model_id="deterministic",
        independence_domain="cortex",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="openspec-archive",
        workflow_phase="ship",
        workflow_repo_root=str(repo),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(bad_job["job_id"], status="exited", exit_code=0)
    registry.bind_workflow_evidence(
        bad_job["job_id"],
        locator={"kind": "ship", "path": "evidence/workflow/bad.json", "hash": "f" * 64},
        subject_head=candidate,
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="policy-commit",
        subject_head=candidate,
    )

    assert manager._manager_archive_applied(run, registry=registry) is False
    with pytest.raises(ValueError, match="missing or ambiguous: openspec-archive"):
        manager._validated_ship_steps(
            registry,
            run=run,
            candidate=candidate,
            coordinator_root=coordinator,
        )


def test_declared_archive_step_ship_audit_rejects_exact_match_job_until_step_passes(
    tmp_path: Path,
) -> None:
    repo, candidate = _repo(
        tmp_path / "ship-audit-declared-step-repo",
        active_change=False,
        archived_change=False,
    )
    snapshot = _snapshot(tmp_path / "snapshot.json")
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=candidate,
        combo="feature-oneshot",
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="openspec-archive",
        subject_head=candidate,
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="policy-commit",
        subject_head=candidate,
    )

    assert manager._manager_archive_applied(run, registry=registry) is False
    with pytest.raises(ValueError, match="missing or ambiguous: openspec-archive"):
        manager._validated_ship_steps(
            registry,
            run=run,
            candidate=candidate,
            coordinator_root=coordinator,
        )


def test_fix_standard_ship_audit_rejects_exact_match_when_archive_jobs_are_ambiguous(
    tmp_path: Path,
) -> None:
    repo, archive_candidate = _repo(
        tmp_path / "ship-audit-ambiguous-repo",
        active_change=False,
        archived_change=False,
    )
    (repo / "repair.txt").write_text("repair\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "repair.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "repair"], check=True)
    final_candidate = _git(repo, "rev-parse", "HEAD")
    sibling_candidate = _git(
        repo,
        "commit-tree",
        _git(repo, "rev-parse", f"{archive_candidate}^{{tree}}"),
        "-p",
        archive_candidate,
        input="unrelated sibling\n",
    )
    snapshot = _snapshot(tmp_path / "snapshot.json")
    coordinator = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=final_candidate,
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="openspec-archive",
        subject_head=final_candidate,
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="openspec-archive",
        subject_head=sibling_candidate,
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=coordinator,
        run=run,
        repo=repo,
        card="policy-commit",
        subject_head=final_candidate,
    )

    assert manager._manager_archive_applied(run, registry=registry) is False
    with pytest.raises(ValueError, match="missing or ambiguous: openspec-archive"):
        manager._validated_ship_steps(
            registry,
            run=run,
            candidate=final_candidate,
            coordinator_root=coordinator,
        )


def test_retry_build_warns_on_active_archive_coexistence_in_exact_candidate_tree(
    tmp_path: Path,
) -> None:
    repo, _initial = _repo(
        tmp_path / "retry-build-warning-repo",
        active_change=True,
        archived_change=False,
    )
    candidate = _coexisting_candidate_commit(repo)
    shutil.rmtree(repo / "openspec" / "changes" / "archive")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=candidate,
        facets=("needs_human",),
        gate_status="failed",
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=tmp_path / "state",
        run=run,
        repo=repo,
        card="openspec-archive",
        subject_head=candidate,
    )

    result = work_actions.execute_work_action(
        args={
            "action": "retry-build",
            "repo": REPO,
            "work_id": WORK_ID,
            "issue": 14,
            "actor": "operator",
            "expected_candidate": candidate,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    reset = registry.get_workflow_run(run.run_id)
    assert result["result"]["reason"] == "candidate-repair-dispatched"
    assert result["result"]["warnings"] == [
        {
            "change": WORK_ID,
            "archive_entries": ["2026-09-14-work"],
            "message": (
                "exact Candidate tree contains both the active OpenSpec change and its "
                "official archive entry; fix the coexistence before the next review"
            ),
        }
    ]
    action = _repair_action_text(reset)
    assert "Preserve the Manager-owned official OpenSpec archive" in action
    assert "Do not recreate the active change" in action
    assert "re-created active change directory" in action
    assert "pre-archive work" not in action


def test_retry_build_warns_on_coexistence_when_archive_jobs_are_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _initial = _repo(
        tmp_path / "retry-build-ambiguous-warning-repo",
        active_change=True,
        archived_change=False,
    )
    candidate = _coexisting_candidate_commit(repo)
    shutil.rmtree(repo / "openspec" / "changes" / "archive")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=candidate,
        facets=("needs_human",),
        gate_status="failed",
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=tmp_path / "state",
        run=run,
        repo=repo,
        card="openspec-archive",
        subject_head=candidate,
    )
    archive_job = next(
        job
        for job in registry.list_jobs()
        if job.get("workflow_card") == "openspec-archive"
    )
    original_list_jobs = registry.list_jobs
    monkeypatch.setattr(
        registry,
        "list_jobs",
        lambda: original_list_jobs()
        + [{**archive_job, "job_id": "ambiguous-archive-job"}],
    )

    assert manager._manager_archive_applied(run, registry=registry) is False
    result = work_actions.execute_work_action(
        args={
            "action": "retry-build",
            "repo": REPO,
            "work_id": WORK_ID,
            "issue": 14,
            "actor": "operator",
            "expected_candidate": candidate,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    assert result["result"]["reason"] == "candidate-repair-dispatched"
    assert result["result"]["warnings"] == [
        {
            "change": WORK_ID,
            "archive_entries": ["2026-09-14-work"],
            "message": (
                "exact Candidate tree contains both the active OpenSpec change and its "
                "official archive entry; fix the coexistence before the next review"
            ),
        }
    ]


def test_retry_build_ignores_dirty_worktree_when_exact_candidate_tree_is_clean(
    tmp_path: Path,
) -> None:
    repo, candidate = _repo(
        tmp_path / "retry-build-clean-tree-repo",
        active_change=True,
        archived_change=False,
    )
    archive = repo / "openspec" / "changes" / "archive" / "2026-09-14-work"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "proposal.md").write_text("# Dirty archive\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=candidate,
        facets=("needs_human",),
        gate_status="failed",
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=tmp_path / "state",
        run=run,
        repo=repo,
        card="openspec-archive",
        subject_head=candidate,
    )

    result = work_actions.execute_work_action(
        args={
            "action": "retry-build",
            "repo": REPO,
            "work_id": WORK_ID,
            "issue": 14,
            "actor": "operator",
            "expected_candidate": candidate,
        },
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
    )

    reset = registry.get_workflow_run(run.run_id)
    assert result["result"]["warnings"] == []
    action = _repair_action_text(reset)
    assert "Preserve the Manager-owned official OpenSpec archive" in action
    assert "pre-archive work" not in action


@pytest.mark.parametrize(
    (
        "failing_pathspec",
        "active_paths",
        "expected_calls",
    ),
    (
        (
            f"openspec/changes/{WORK_ID}/",
            (),
            (f"openspec/changes/{WORK_ID}/",),
        ),
        (
            "openspec/changes/archive/",
            (f"openspec/changes/{WORK_ID}/proposal.md",),
            (
                f"openspec/changes/{WORK_ID}/",
                "openspec/changes/archive/",
            ),
        ),
        (
            "openspec/changes/archive/",
            (),
            (
                f"openspec/changes/{WORK_ID}/",
                "openspec/changes/archive/",
            ),
        ),
    ),
)
def test_candidate_tree_matching_archive_entries_raises_on_tree_probe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_pathspec: str,
    active_paths: tuple[str, ...],
    expected_calls: tuple[str, ...],
) -> None:
    repo, candidate = _repo(
        tmp_path / "candidate-tree-matching-archive-entries-repo",
        active_change=True,
        archived_change=False,
    )
    candidate_head = candidate
    candidate_tree_calls: list[str] = []
    active_pathspec = f"openspec/changes/{WORK_ID}/"

    def fake_candidate_tree_paths(
        *, workspace_root: Path, candidate: str, pathspec: str
    ) -> tuple[str, ...]:
        candidate_tree_calls.append(pathspec)
        assert workspace_root == repo
        assert candidate == candidate_head
        if pathspec == failing_pathspec:
            raise RuntimeError("retry-build exact candidate tree inspection failed")
        if pathspec == active_pathspec:
            return active_paths
        if pathspec == "openspec/changes/archive/":
            return (f"openspec/changes/archive/2026-09-14-{WORK_ID}/proposal.md",)
        raise AssertionError(f"unexpected pathspec {pathspec}")

    monkeypatch.setattr(work_actions, "_candidate_tree_paths", fake_candidate_tree_paths)

    with pytest.raises(RuntimeError, match="retry-build.*candidate tree"):
        work_actions._candidate_tree_matching_archive_entries(
            workspace_root=repo,
            candidate=candidate,
            change=WORK_ID,
        )
    assert tuple(candidate_tree_calls) == expected_calls


@pytest.mark.parametrize(
    ("combo", "failing_pathspec", "expected_calls"),
    (
        (
            "fix-standard",
            f"openspec/changes/{WORK_ID}/",
            [f"openspec/changes/{WORK_ID}/"],
        ),
        (
            "fix-standard",
            "openspec/changes/archive/",
            [
                f"openspec/changes/{WORK_ID}/",
                "openspec/changes/archive/",
            ],
        ),
        (
            "feature-oneshot",
            "openspec/changes/archive/",
            [
                f"openspec/changes/{WORK_ID}/",
                "openspec/changes/archive/",
            ],
        ),
    ),
)
def test_retry_build_tree_inspection_failure_raises_before_reset_for_each_combo_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    combo: str,
    failing_pathspec: str,
    expected_calls: list[str],
) -> None:
    repo, candidate = _repo(
        tmp_path / "retry-build-tree-failure-source",
        active_change=True,
        archived_change=False,
    )
    snapshot = _snapshot(tmp_path / "snapshot.json")
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _create_fix_standard_run(
        registry=registry,
        repo_root=repo,
        snapshot=snapshot,
        candidate=candidate,
        combo=combo,
        facets=("needs_human",),
        gate_status="failed",
    )
    candidate_tree_calls: list[str] = []
    active_pathspec = f"openspec/changes/{WORK_ID}/"

    def fake_candidate_tree_paths(
        *, workspace_root: Path, candidate: str, pathspec: str
    ) -> tuple[str, ...]:
        candidate_tree_calls.append(pathspec)
        assert workspace_root == repo
        assert candidate == run.candidate_head
        if pathspec == failing_pathspec:
            raise RuntimeError("retry-build exact candidate tree inspection failed")
        if pathspec == active_pathspec:
            return ()
        if pathspec == "openspec/changes/archive/":
            return ()
        raise AssertionError(f"unexpected pathspec {pathspec}")

    monkeypatch.setattr(work_actions, "_candidate_tree_paths", fake_candidate_tree_paths)
    reset_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    original_reset = registry._manager_reset_workflow_for_retry_build

    def recording_reset(*args, **kwargs):
        reset_calls.append((args, kwargs))
        return original_reset(*args, **kwargs)

    registry._manager_reset_workflow_for_retry_build = recording_reset  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="retry-build.*candidate tree"):
        work_actions.execute_work_action(
            args={
                "action": "retry-build",
                "repo": REPO,
                "work_id": WORK_ID,
                "issue": 14,
                "actor": "operator",
                "expected_candidate": candidate,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )
    assert reset_calls == []
    assert candidate_tree_calls == expected_calls


@pytest.mark.parametrize("stream", ("stdout", "stderr"))
def test_ship_validator_rejects_aborted_archive_without_recording_manager_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=True,
        archived_change=False,
    )
    _use_fix_standard_ship_steps(harness)
    runner_kwargs = {
        "repo": harness.worktree,
        "move_archive": False,
        "returncode": 0,
        stream: "Aborted. No files were changed.",
    }
    runner = _ArchiveCommandRunner(**runner_kwargs)
    monkeypatch.setattr(work_bridge, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(work_bridge, "GitHubDeliveryClient", FakeGitHubDeliveryClient)
    _swap_ship_runner(harness, runner)

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)
    updated = harness.registry.get_workflow_run(harness.run_id)
    archive_jobs = [
        job
        for job in harness.registry.list_jobs()
        if job.get("workflow_run_id") == harness.run_id
        and job.get("workflow_card") == "openspec-archive"
    ]

    assert isinstance(outcome.exception, RuntimeError)
    assert "official OpenSpec archive aborted" in str(outcome.exception)
    assert updated.candidate_head == harness.candidate
    assert archive_jobs == []
    assert (
        job_workspace.source_branch_head(harness.repo, "feature/14-work")
        == harness.candidate
    )


def test_ship_validator_requires_archive_relocation_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=True,
        archived_change=False,
    )
    _use_fix_standard_ship_steps(harness)
    runner = _ArchiveCommandRunner(
        repo=harness.worktree,
        move_archive=False,
        returncode=0,
    )
    monkeypatch.setattr(work_bridge, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(work_bridge, "GitHubDeliveryClient", FakeGitHubDeliveryClient)
    _swap_ship_runner(harness, runner)

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)
    updated = harness.registry.get_workflow_run(harness.run_id)
    archive_jobs = [
        job
        for job in harness.registry.list_jobs()
        if job.get("workflow_run_id") == harness.run_id
        and job.get("workflow_card") == "openspec-archive"
    ]

    assert isinstance(outcome.exception, RuntimeError)
    assert "official OpenSpec archive relocation missing" in str(outcome.exception)
    assert updated.candidate_head == harness.candidate
    assert archive_jobs == []
    assert (
        job_workspace.source_branch_head(harness.repo, "feature/14-work")
        == harness.candidate
    )


@pytest.mark.parametrize("lingering_active_change", ("file", "broken-symlink"))
def test_ship_validator_rejects_lingering_active_change_path_after_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lingering_active_change: str,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=True,
        archived_change=False,
    )
    _use_fix_standard_ship_steps(harness)
    runner = _ArchiveCommandRunner(
        repo=harness.worktree,
        move_archive=True,
        lingering_active_change=lingering_active_change,
    )
    monkeypatch.setattr(work_bridge, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(work_bridge, "GitHubDeliveryClient", FakeGitHubDeliveryClient)
    _swap_ship_runner(harness, runner)

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)
    updated = harness.registry.get_workflow_run(harness.run_id)
    archive_jobs = [
        job
        for job in harness.registry.list_jobs()
        if job.get("workflow_run_id") == harness.run_id
        and job.get("workflow_card") == "openspec-archive"
    ]

    assert isinstance(outcome.exception, RuntimeError)
    assert "official OpenSpec archive relocation missing" in str(outcome.exception)
    assert updated.candidate_head == harness.candidate
    assert archive_jobs == []
    assert (
        job_workspace.source_branch_head(harness.repo, "feature/14-work")
        == harness.candidate
    )


def test_ship_validator_rejects_recreated_active_change_after_archive_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def repo_with_coexisting_archive(root: Path, *, active_change: bool, archived_change: bool):
        assert active_change is True
        assert archived_change is False
        repo, _candidate = _repo(root, active_change=True, archived_change=False)
        _coexisting_candidate_commit(repo)
        subprocess.run(["git", "-C", str(repo), "branch", "-f", "feature/14-work", "HEAD"], check=True)
        return repo, _git(repo, "rev-parse", "HEAD")

    monkeypatch.setattr("test_preflight_closeout_order._repo", repo_with_coexisting_archive)
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=True,
        archived_change=False,
    )
    _use_fix_standard_ship_steps(harness)
    _record_manager_ship_job(
        registry=harness.registry,
        state_root=harness.state_root,
        run=harness.run,
        repo=harness.repo,
        card="openspec-archive",
        subject_head=harness.candidate,
    )

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)

    assert isinstance(outcome.exception, RuntimeError)
    assert "re-created active OpenSpec change" in str(outcome.exception)
    assert not harness.runner.saw("openspec", "archive")
