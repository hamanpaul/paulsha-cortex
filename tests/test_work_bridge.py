from __future__ import annotations

import json
import hashlib
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

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
    evaluation["outputs"] = []
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
            "outputs": [],
            "output_baseline": [],
        },
        "payload": evaluation,
        "artifacts": [],
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
    validator = work_bridge.build_production_ship_validator(
        registry=registry,
        coordinator_root=tmp_path / "state",
        snapshot_path=snapshot,
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
    assert not (tmp_path / "state" / "delivery-journal.json").exists()


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
    capabilities: []
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
                report = repo / report_ref
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    "---\n"
                    f"workflow_run_id: {contract['run_id']}\n"
                    f"workflow_card_id: {card}\n"
                    f"candidate: {candidate}\n"
                    "---\n# Verification\n\nPassed.\n",
                    encoding="utf-8",
                )
                evidence = {
                    "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
                    "slice_id": f"{contract['run_id']}-{card}",
                    "candidate": candidate,
                    "status": "verified",
                    "summary": "ok",
                    "details": {"card": card},
                    "outputs": [report_ref],
                }
            else:
                report_ref = (
                    "reports/review/work-adversarial.md"
                    if card == "adversarial-review"
                    else "reports/review/work.md"
                )
                report = repo / report_ref
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    "---\n"
                    f"workflow_run_id: {contract['run_id']}\n"
                    f"workflow_card_id: {card}\n"
                    f"candidate: {candidate}\n"
                    "---\n# Review\n\nPassed.\n",
                    encoding="utf-8",
                )
                builder = registry.get_job(contract["builder_job_id"])
                evidence = review.build_gate_evaluation(
                    slice_id=f"{contract['run_id']}-{card}",
                    state="passed",
                    reason="accepted",
                    builder_job_id=builder["job_id"],
                    reviewer_job_id=slice_id,
                    candidate=candidate,
                    launch_identity={
                        "builder": {
                            "executor": builder["executor"],
                            "model_id": builder["model_id"],
                            "independence_domain": builder["independence_domain"],
                        },
                        "reviewer": {
                            "executor": job["executor"],
                            "model_id": job["model_id"],
                            "independence_domain": job["independence_domain"],
                        },
                    },
                )
                evidence["outputs"] = [report_ref]
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
    executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=Launcher(),
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
