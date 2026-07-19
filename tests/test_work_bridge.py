from __future__ import annotations

import json
import hashlib
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import manager_daemon, review, verification, work_bridge
from paulsha_cortex.coordinator.claim import load_work_authority, work_authority_digest
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import (
    GateEvidenceRef,
    PlanningArtifactAuthority,
    WorkflowStep,
)


def _repo(root: Path) -> tuple[Path, str]:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "git@github.com:acme/demo.git"],
        check=True,
    )
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, head


def _snapshot(path: Path) -> Path:
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
                        "work_id": "work",
                        "mapped_issues": [14],
                        "mapped_prs": [],
                        "mapped_openspec": ["work"],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": False,
                        "source_revisions": [
                            "github_issue:acme/demo#14@issue-open",
                            "openspec:acme/demo:work@spec-1",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _steps() -> tuple[WorkflowStep, ...]:
    personas = {
        "claim": "manager",
        "define": "planner",
        "plan": "planner",
        "build": "builder",
        "verify": "reviewer",
        "review": "reviewer",
        "ship": "manager",
    }
    return tuple(
        WorkflowStep(
            phase=phase,
            persona=persona,
            card=f"{phase}-card",
            executor=("codex" if phase == "build" else "claude"),
            model=("gpt" if phase == "build" else "sonnet"),
            domain=("openai" if phase == "build" else "anthropic"),
            inputs=(),
            outputs=(),
            gate_result="pending" if phase == "ship" else "passed",
        )
        for phase, persona in personas.items()
    )


def test_ship_adapter_creates_pr_after_metadata_preflight_and_binds_same_run(
    monkeypatch, tmp_path: Path
) -> None:
    repo, candidate = _repo(tmp_path / "repo")
    plan = repo / "docs" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Accepted plan\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = load_work_authority(repo="acme/demo", work_id="work", snapshot_path=snapshot)
    registry = JobRegistry(state_path=tmp_path / "state" / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="work",
        repo="acme/demo",
        claim_key="claim:v1:" + "1" * 64,
        source_revision=work_authority_digest(authority),
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="review",
        steps=_steps(),
        issue_refs=("acme/demo#14",),
        openspec_refs=("work",),
        pr_refs=(),
        attempts={"review": 1},
        gate_refs=(),
        candidate_head=candidate,
        verified_head=candidate,
        gate_status="running",
        planning_authority=(
            PlanningArtifactAuthority("docs/spec.md", "spec", "work", "3" * 64),
            PlanningArtifactAuthority("docs/plan.md", "plan", "work", "4" * 64),
        ),
    )
    job = registry.create_job(
        task="wf-build",
        persona="builder",
        kind="build",
        branch="feature/14-work",
        pane="",
        worktree=str(repo),
        dispatch_head=candidate,
        executor="codex",
        model_id="gpt",
        independence_domain="openai",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="build-card",
        workflow_phase="build",
        workflow_repo_root=str(repo),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    report_ref = "reports/review/work-review.md"
    review_job = registry.create_job(
        task="wf-review",
        persona="reviewer",
        kind="review",
        branch="feature/14-work",
        pane="",
        worktree=str(repo),
        executor="claude",
        model_id="sonnet",
        independence_domain="anthropic",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="review-card",
        workflow_phase="review",
        workflow_repo_root=str(repo),
        workflow_outputs=(report_ref,),
        workflow_output_baseline=(),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)
    evaluation = review.build_gate_evaluation(
        slice_id=f"{run.run_id}-review-card",
        state="passed",
        reason="accepted",
        builder_job_id=job["job_id"],
        reviewer_job_id=review_job["job_id"],
        candidate=candidate,
        launch_identity={
            "builder": {
                "executor": "codex",
                "model_id": "gpt",
                "independence_domain": "openai",
            },
            "reviewer": {
                "executor": "claude",
                "model_id": "sonnet",
                "independence_domain": "anthropic",
            },
        },
    )
    report = repo / report_ref
    report.parent.mkdir(parents=True)
    report.write_text("# Canonical review\n", encoding="utf-8")
    report_hash = hashlib.sha256(report.read_bytes()).hexdigest()
    evaluation["outputs"] = [{"path": report_ref, "sha256": report_hash}]
    review_job = registry.get_job(review_job["job_id"])
    envelope = {
        "schema_version": 1,
        "kind": "review",
        "job": {
            "job_id": review_job["job_id"],
            "run_id": run.run_id,
            "claim_key": run.claim_key,
            "repo": run.repo,
            "source_revision": run.source_revision,
            "card_id": "review-card",
            "phase": "review",
            "inputs": [],
            "outputs": [report_ref],
            "output_baseline": [],
        },
        "payload": evaluation,
        "artifacts": [
            {"path": report_ref, "sha256": report_hash, "baseline_sha256": None}
        ],
    }
    content = (
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    foreign = tmp_path / "state" / "evidence" / "workflow" / "foreign.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(content)
    foreign_hash = hashlib.sha256(content).hexdigest()
    registry.bind_workflow_evidence(
        review_job["job_id"],
        locator={
            "kind": "review",
            "path": "evidence/workflow/foreign.json",
            "hash": foreign_hash,
        },
        subject_head=candidate,
    )
    run = registry._manager_update_workflow_run(
        run.run_id,
        gate_refs=(GateEvidenceRef("foreign-review", str(foreign), foreign_hash),),
    )

    preflight_requests = []
    monkeypatch.setattr(work_bridge, "load_preflight_command", lambda: ("preflight",))

    def fake_preflight(**kwargs):
        preflight_requests.append(kwargs["request"])
        preflight_root = Path(kwargs["repo_root"])
        assert preflight_root != repo
        assert subprocess.run(
            ["git", "-C", str(preflight_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout == ""
        assert subprocess.run(
            ["git", "-C", str(preflight_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == candidate
        assert subprocess.run(
            ["git", "-C", str(preflight_root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip().startswith("feature/preflight-")
        return PreflightResult(
            True,
            None,
            CommandResult(("policy",), 0, "", ""),
            CommandResult(("preflight",), 0, "", ""),
            candidate,
            "5" * 40,
        )

    monkeypatch.setattr(work_bridge, "run_preflight", fake_preflight)
    created = []

    class GitHub:
        def __init__(self, *, runner):
            pass

        def create_or_get_pull_request(self, **kwargs):
            created.append(kwargs)
            return 17

        def fetch_remote_closure(self, **kwargs):
            return SimpleNamespace(default_head="d" * 40, merge_commit="e" * 40)

        def fetch_default_branch(self, **kwargs):
            return "main"

    monkeypatch.setattr(work_bridge, "GitHubDeliveryClient", GitHub)
    pushed = False

    def delivery_runner(argv, **kwargs):
        nonlocal pushed
        if "ls-remote" in argv:
            return SimpleNamespace(
                returncode=0 if pushed else 2,
                stdout=(f"{candidate}\trefs/heads/feature/14-work\n" if pushed else ""),
                stderr="",
            )
        if "push" in argv:
            assert argv[-3:] == ["push", "origin", "HEAD:refs/heads/feature/14-work"]
            pushed = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    validator = work_bridge.build_production_ship_validator(
        registry=registry,
        coordinator_root=tmp_path / "state",
        snapshot_path=snapshot,
        runner=delivery_runner,
    )

    result = validator(run=run, candidate=candidate)

    assert result["status"] == "pending"
    assert preflight_requests[0].metadata_path is not None
    assert preflight_requests[0].pr_number is None
    assert created[0]["branch"] == "feature/14-work"
    updated = registry.get_workflow_run(run.run_id)
    assert updated.run_id == run.run_id
    assert updated.pr_refs == ("acme/demo#17",)
    assert updated.source_revision != run.source_revision
    assert not report.exists()
    assert plan.is_file()
    assert subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "feature/preflight-*"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    journal = json.loads(
        (tmp_path / "state" / "delivery-journal.json").read_text(encoding="utf-8")
    )
    assert journal["runs"][run.run_id]["pushes"][candidate]["head"] == candidate


def test_delivery_report_cleanup_rejects_hash_drift_without_deleting(
    tmp_path: Path,
) -> None:
    repo, candidate = _repo(tmp_path / "repo")
    state_root = tmp_path / "state"
    report_ref = "reports/review/work-review.md"
    report = repo / report_ref
    report.parent.mkdir(parents=True)
    canonical = b"# Historical canonical review\n"
    latest = b"# Latest canonical review\n"
    report.write_bytes(canonical)
    run = SimpleNamespace(run_id="workflow-run", candidate_head=candidate)
    job = {
        "job_id": "review-job",
        "workflow_run_id": run.run_id,
        "workflow_claim_key": "claim:v1:" + "1" * 64,
        "workflow_repo": "acme/demo",
        "workflow_card": "review-card",
        "workflow_phase": "review",
        "workflow_inputs": [],
        "workflow_outputs": [report_ref],
        "workflow_output_baseline": [],
        "source_revision": "source-revision",
        "subject_head": candidate,
        "status": "exited",
        "exit_code": 0,
    }
    envelope = {
        "schema_version": 1,
        "kind": "review",
        "job": {
            "job_id": job["job_id"],
            "run_id": run.run_id,
            "claim_key": job["workflow_claim_key"],
            "repo": job["workflow_repo"],
            "source_revision": job["source_revision"],
            "card_id": job["workflow_card"],
            "phase": "review",
            "inputs": [],
            "outputs": [report_ref],
            "output_baseline": [],
        },
        "payload": {},
        "artifacts": [
            {
                "path": report_ref,
                "sha256": hashlib.sha256(canonical).hexdigest(),
                "baseline_sha256": None,
            }
        ],
    }
    content = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    evidence = state_root / "evidence" / "workflow" / "review.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(content)
    job["workflow_evidence"] = {
        "kind": "review",
        "path": "evidence/workflow/review.json",
        "hash": hashlib.sha256(content).hexdigest(),
    }
    latest_job = {
        **job,
        "job_id": "review-job-2",
        "workflow_card": "adversarial-review-card",
        "workflow_output_baseline": [
            {"path": report_ref, "sha256": hashlib.sha256(canonical).hexdigest()}
        ],
    }
    latest_envelope = {
        **envelope,
        "job": {
            **envelope["job"],
            "job_id": latest_job["job_id"],
            "card_id": latest_job["workflow_card"],
            "output_baseline": latest_job["workflow_output_baseline"],
        },
        "artifacts": [
            {
                "path": report_ref,
                "sha256": hashlib.sha256(latest).hexdigest(),
                "baseline_sha256": hashlib.sha256(canonical).hexdigest(),
            }
        ],
    }
    latest_content = (
        json.dumps(latest_envelope, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    latest_evidence = state_root / "evidence" / "workflow" / "review-2.json"
    latest_evidence.write_bytes(latest_content)
    latest_job["workflow_evidence"] = {
        "kind": "review",
        "path": "evidence/workflow/review-2.json",
        "hash": hashlib.sha256(latest_content).hexdigest(),
    }

    class Registry:
        @staticmethod
        def list_jobs():
            return [job, latest_job]

    with pytest.raises(RuntimeError, match="report hash drift"):
        work_bridge._remove_canonical_untracked_reports(
            registry=Registry(),
            state_root=state_root,
            run=run,
            worktree=repo,
        )

    assert report.read_bytes() == canonical


def test_archive_commit_pushes_new_candidate_and_invalidates_old_gates(
    tmp_path: Path,
) -> None:
    repo, _initial = _repo(tmp_path / "repo")
    active = repo / "openspec" / "changes" / "work"
    active.mkdir(parents=True)
    (active / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add change"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = load_work_authority(repo="acme/demo", work_id="work", snapshot_path=snapshot)
    state_root = tmp_path / "state"
    registry = JobRegistry(state_path=state_root / "jobs.json")
    ship_steps = (
        WorkflowStep(
            "ship", "manager", "openspec-archive", None, None, None, (), ()
        ),
        WorkflowStep("ship", "manager", "policy-commit", None, None, None, (), ()),
    )
    run = registry._manager_create_workflow_run(
        work_id="work",
        repo="acme/demo",
        claim_key="claim:v1:" + "1" * 64,
        source_revision=work_authority_digest(authority),
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="review",
        steps=tuple(step for step in _steps() if step.phase != "ship") + ship_steps,
        issue_refs=("acme/demo#14",),
        openspec_refs=("work",),
        pr_refs=(),
        attempts={"verify": 1, "review": 1},
        gate_refs=(GateEvidenceRef("foreign-review", "old-review", "1" * 64),),
        candidate_head=candidate,
        verified_head=candidate,
        gate_status="running",
    )

    archive = repo / "openspec" / "changes" / "archive"
    archive.mkdir(parents=True)
    active.rename(archive / "work")
    with (repo / "CHANGELOG.md").open("a", encoding="utf-8") as handle:
        handle.write("- Archive work.\n")

    remote_head: str | None = None

    def delivery_runner(argv, **kwargs):
        nonlocal remote_head
        if "ls-remote" in argv:
            if remote_head is None:
                return SimpleNamespace(returncode=2, stdout="", stderr="")
            return SimpleNamespace(
                returncode=0,
                stdout=f"{remote_head}\trefs/heads/feature/14-work\n",
                stderr="",
            )
        if "push" in argv:
            remote_head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    reset = work_bridge._commit_archive_and_require_reverification(
        registry=registry,
        state_root=state_root,
        run=run,
        authority=authority,
        worktree=repo,
        branch="feature/14-work",
        candidate=candidate,
        runner=delivery_runner,
    )

    assert reset.current_phase == "verify"
    assert reset.candidate_head == remote_head
    assert reset.candidate_head != candidate
    assert reset.verified_head is None
    assert reset.gate_refs == ()
    assert reset.attempts["verify"] == 2
    assert all(
        step.gate_result == "pending"
        for step in reset.steps
        if step.phase in {"verify", "review"}
    )
    assert next(
        step for step in reset.steps if step.card == "openspec-archive"
    ).gate_result == "passed"
    archive_jobs = [
        job for job in registry.list_jobs()
        if job.get("workflow_card") == "openspec-archive"
    ]
    assert len(archive_jobs) == 1
    assert archive_jobs[0]["subject_head"] == reset.candidate_head
    assert archive_jobs[0]["workflow_evidence"]["hash"]

    def bind_verify(subject: str, marker: str) -> None:
        job = registry.create_job(
            task=f"verify-{marker}",
            persona="reviewer",
            kind="review",
            branch="feature/14-work",
            pane="",
            worktree=str(repo),
            executor="claude",
            model_id="sonnet",
            independence_domain="anthropic",
            subject_head=subject,
            workflow_run_id=run.run_id,
            workflow_claim_key=run.claim_key,
            workflow_repo=run.repo,
            workflow_card="verify-card",
            workflow_phase="verify",
            workflow_repo_root=str(repo),
            source_revision=run.source_revision,
        )
        registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
        job = registry.get_job(job["job_id"])
        envelope = {
            "schema_version": 1,
            "kind": "verify",
            "job": {
                "job_id": job["job_id"],
                "run_id": job["workflow_run_id"],
                "claim_key": job["workflow_claim_key"],
                "repo": job["workflow_repo"],
                "source_revision": job["source_revision"],
                "card_id": job["workflow_card"],
                "phase": job["workflow_phase"],
                "inputs": job["workflow_inputs"],
                "outputs": job["workflow_outputs"],
                "output_baseline": job["workflow_output_baseline"],
            },
            "payload": {"candidate": subject, "marker": marker},
            "artifacts": [],
        }
        content = (
            json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        relative = Path("evidence") / "workflow" / f"verify-{marker}.json"
        path = state_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        registry.bind_workflow_evidence(
            job["job_id"],
            locator={
                "kind": "verify",
                "path": relative.as_posix(),
                "hash": hashlib.sha256(content).hexdigest(),
            },
            subject_head=subject,
        )

    bind_verify(candidate, "old")
    with pytest.raises(RuntimeError, match="one canonical verify"):
        work_bridge._workflow_evidence_payload(
            registry=registry,
            state_root=state_root,
            run=reset,
            phase="verify",
        )
    bind_verify(reset.candidate_head, "current")
    payload, selected = work_bridge._workflow_evidence_payload(
        registry=registry,
        state_root=state_root,
        run=reset,
        phase="verify",
    )
    assert payload["marker"] == "current"
    assert selected["subject_head"] == reset.candidate_head


def test_installed_defaults_start_to_ship_handoff_remains_monitor_ongoing(
    monkeypatch, tmp_path: Path
) -> None:
    """Exercise installed env paths without injecting snapshot/state/repo factories."""

    repo, _initial = _repo(tmp_path / "repo")
    planning_files = {
        "docs/superpowers/specs/work-spec.md": (
            "---\nstatus: accepted\n---\n# Spec\n## Requirements\nReady.\n"
        ),
        "docs/superpowers/specs/work-design.md": (
            "---\nstatus: accepted\n---\n# Design\n## Decisions\nReady.\n"
        ),
        "docs/superpowers/plans/work-plan.md": (
            "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n"
        ),
        "openspec/changes/work/proposal.md": (
            "---\nstatus: accepted\n---\n# Proposal\n## Requirements\nReady.\n"
        ),
        "openspec/changes/work/design.md": (
            "---\nstatus: accepted\n---\n# Design\n## Decisions\nReady.\n"
        ),
        "openspec/changes/work/tasks.md": (
            "---\nstatus: accepted\n---\n# Tasks\n- [ ] Ship.\n"
        ),
        "docs/todo.md": "---\nstatus: accepted\n---\n# Todo\n- [ ] Ship.\n",
    }
    for ref, body in planning_files.items():
        target = repo / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add planning"], check=True)
    candidate = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    agents = tmp_path / "agents"
    monitor_root = agents / "monitor"
    coordinator_root = agents / "coordinator"
    config_root = agents / "config" / "paulsha"
    monitor_root.mkdir(parents=True)
    config_root.mkdir(parents=True)
    snapshot = monitor_root / "work-items.snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-live",
                        "last_success_epoch": time.time(),
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "work",
                        "mapped_issues": [14],
                        "mapped_prs": [],
                        "mapped_openspec": ["work"],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": False,
                        "source_revisions": [
                            "github_issue:acme/demo#14@issue-open",
                            "superpowers_spec:acme/demo:docs/superpowers/specs/work-spec.md@spec-1",
                            "superpowers_plan:acme/demo:docs/superpowers/plans/work-plan.md@plan-1",
                            "openspec:acme/demo:work@change-1",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monitor_config = config_root / "project-cortex.yaml"
    monitor_config.write_text(
        f"workspaces:\n  - name: demo\n    path: {repo}\n",
        encoding="utf-8",
    )
    (config_root / "model-identities.yaml").write_text(
        """schema_version: 2
identities:
  - executor: codex
    model_id: gpt-primary
    independence_domain: openai
    capabilities: [planning]
  - executor: claude
    model_id: claude-reviewer
    independence_domain: anthropic
    capabilities: [review]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(agents))
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(coordinator_root))
    monkeypatch.setenv("PSC_MONITOR_STATE_ROOT", str(monitor_root))
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(config_root))
    monkeypatch.setenv("PSC_MONITOR_CONFIG", str(monitor_config))

    registry = JobRegistry()
    branches: list[str] = []

    class WorktreeCreator:
        def create(self, branch, base_sha=None):
            branches.append(branch)
            return str(repo)

    dispatcher = Dispatcher(
        registry,
        pane_sender=None,
        worktree_creator=WorktreeCreator(),
        git_runner=None,
    )

    class Launcher:
        def as_read_only(self):
            return self

        def as_commit_required(self):
            return self

        def as_review_only(self, *, terminal_kind):
            assert terminal_kind in {
                "workflow-verification-result", "workflow-review-result",
            }
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            contract = json.loads(prompt.split("Contract: ", 1)[1])
            job = registry.get_job(slice_id)
            phase = contract["phase"]
            card = contract["card_id"]
            if phase == "plan":
                evidence = {
                    "schema_version": 1,
                    "kind": "workflow-card",
                    "status": "passed",
                    "run_id": contract["run_id"],
                    "card_id": card,
                    "candidate": None,
                    "outputs": ["docs/superpowers/plans/work-plan.md"],
                }
            elif phase == "build":
                evidence = {
                    "schema_version": 1,
                    "kind": "workflow-card",
                    "status": "passed",
                    "run_id": contract["run_id"],
                    "card_id": card,
                    "candidate": candidate,
                    "outputs": [],
                }
            elif phase == "verify":
                report_ref = "reports/verify/work.md"
                evidence = {
                    "schema_version": 1,
                    "kind": "workflow-verification-result",
                    "status": "verified",
                    "summary": "ok",
                    "details": {"card": card},
                    "reports": [{"path": report_ref, "body": "# Verification\n\nPassed."}],
                }
            else:
                report_ref = (
                    "reports/review/work-adversarial.md"
                    if card == "adversarial-review"
                    else "reports/review/work.md"
                )
                evidence = {
                    "schema_version": 1,
                    "kind": "workflow-review-result",
                    "reason": "accepted",
                    "findings": [],
                    "reports": [{"path": report_ref, "body": "# Review\n\nPassed."}],
                }
            log_path = Path(log_dir) / f"{slice_id}.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
            log_path.with_suffix(".exit").write_text("0", encoding="utf-8")
            return LaunchHandle(
                executor=str(job["executor"]),
                model_id=str(job["model_id"]),
                session_name=slice_id,
                pid=100,
                log_path=str(log_path),
            )

    preflight_requests = []
    monkeypatch.setattr(work_bridge, "load_preflight_command", lambda: ("preflight",))

    def fake_preflight(**kwargs):
        preflight_requests.append(kwargs["request"])
        return PreflightResult(
            True,
            None,
            CommandResult(("policy",), 0, "", ""),
            CommandResult(("preflight",), 0, "", ""),
            candidate,
            "5" * 40,
        )

    monkeypatch.setattr(work_bridge, "run_preflight", fake_preflight)
    created = []

    class GitHub:
        def __init__(self, *, runner):
            pass

        def create_or_get_pull_request(self, **kwargs):
            created.append(kwargs)
            return 17

        def fetch_remote_closure(self, **kwargs):
            return SimpleNamespace(default_head="d" * 40, merge_commit="e" * 40)

        def fetch_default_branch(self, **kwargs):
            return "main"

    monkeypatch.setattr(work_bridge, "GitHubDeliveryClient", GitHub)
    pushed = False

    def delivery_runner(argv, **kwargs):
        nonlocal pushed
        if "ls-remote" in argv:
            return SimpleNamespace(
                returncode=0 if pushed else 2,
                stdout=(f"{candidate}\trefs/heads/feature/14-work\n" if pushed else ""),
                stderr="",
            )
        if "push" in argv:
            assert argv[-3:] == ["push", "origin", "HEAD:refs/heads/feature/14-work"]
            pushed = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    ship_validator = work_bridge.build_production_ship_validator(
        registry=registry,
        coordinator_root=coordinator_root,
        runner=delivery_runner,
    )
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=Launcher(),
        workflow_ship_validator=ship_validator,
    )
    started = executor(
        build_request(
            req_type="work-action",
            args={"action": "start", "repo": "acme/demo", "work_id": "work"},
            requested_by="operator",
        )
    )
    run_id = started["result"]["run"]["run_id"]
    first_job = registry.get_job(started["result"]["job_id"])
    assert first_job["persona"] == "planner"

    result = None
    for _ in range(10):
        result = executor(
            build_request(
                req_type="workflow-action",
                args={"action": "resume", "run_id": run_id},
                requested_by="operator",
            )
        )
        if registry.get_workflow_run(run_id).pr_refs:
            break
    assert result is not None and result["reason"] == "delivery-in-progress"
    run = registry.get_workflow_run(run_id)
    assert run.current_phase == "review"
    assert run.pr_refs == ("acme/demo#17",)
    assert branches == ["feature/14-work"]
    assert preflight_requests[0].metadata_path is not None
    assert preflight_requests[0].pr_number is None
    assert created[0]["branch"] == "feature/14-work"

    from paulsha_cortex.monitor.providers import WorkflowRegistryProvider

    monitor = WorkflowRegistryProvider("acme/demo").scan()
    workflow_sources = [source for source in monitor.sources if source.kind == "workflow_run"]
    assert monitor.status == "ok"
    assert len(workflow_sources) == 1
    assert workflow_sources[0].status == "ongoing"

    # A post-merge retry can derive the CompletionRecord draft from the same
    # run's canonical verify/review jobs; no delivery-owned lifecycle row is
    # needed to reconstruct workflow truth.
    build_jobs = [
        job
        for job in registry.list_jobs()
        if job.get("workflow_run_id") == run_id and job.get("workflow_phase") == "build"
    ]
    assert len(build_jobs) == 3
    assert {job["dispatch_head"] for job in build_jobs} == {candidate}
    foreign_ref = next(ref for ref in run.gate_refs if ref.kind == "foreign-review")
    authorization_path = coordinator_root / "evidence" / "merge-authorization.json"
    authorization_path.parent.mkdir(parents=True, exist_ok=True)
    authorization_path.write_text("{}\n", encoding="utf-8")
    authorization = {
        "payload": {
            "head": candidate,
            "tree_hash": "5" * 40,
            "foreign_review_path": foreign_ref.ref,
            "foreign_review_hash": foreign_ref.sha256,
            "preflight_hash": "6" * 64,
            "copilot_review_id": 91,
            "copilot_hash": "7" * 64,
        },
        "path": str(authorization_path),
        "hash": "8" * 64,
    }
    (coordinator_root / "delivery-journal.json").write_text(
        json.dumps(
            {
                "schema": "cortex-delivery-journal/v1",
                "runs": {
                    run_id: {
                        "workflow_step_ids": [
                            f"{run_id}:{step.phase}:{step.card}" for step in run.steps
                        ],
                        "ship": {
                            "phase": "merged",
                            "merge_authorization": authorization,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    authority = work_bridge._authority_with_manager_pr(
        load_work_authority(repo="acme/demo", work_id="work"), 17
    )
    draft = work_bridge._completion_draft(
        registry=registry,
        state_root=coordinator_root,
        run=run,
        authority=authority,
        candidate=candidate,
        pr_number=17,
        foreign_ref=foreign_ref,
        runner=subprocess.run,
        now=time.time,
    )
    assert draft is not None and draft.is_file()
    completion_payload = json.loads(draft.read_text(encoding="utf-8"))
    assert completion_payload["work_authority"]["run_id"] == run_id
    assert completion_payload["work_authority"]["merge_commit"] == "e" * 40
