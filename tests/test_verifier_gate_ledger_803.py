from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import gate_ledger, manager, terminal_contract
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


_CANDIDATE = "a" * 40


def _write_build_ledger(
    tmp_path: Path,
    *,
    ledger_head: str = _CANDIDATE,
    exit_code: int = 0,
) -> tuple[dict[str, object], dict[str, object], Path]:
    log_path = tmp_path / "build.jsonl"
    ledger_path = terminal_contract.gate_ledger_path(log_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    payload = gate_ledger.build_ledger(
        [
            {
                "name": "pytest",
                "command": "python3 -m pytest -q",
                "exit_code": exit_code,
                "status": "passed" if exit_code == 0 else "failed",
                "detail": "",
            }
        ],
        slice_id="build-job",
        worktree_state={"head": ledger_head, "probe": "ok"},
    )
    ledger_path.write_text(gate_ledger.encode_ledger(payload), encoding="utf-8")
    job: dict[str, object] = {
        "job_id": "build-job",
        "persona": "builder",
        "workflow_run_id": "run-803",
        "workflow_phase": "build",
        "status": "exited",
        "exit_code": 0,
        "subject_head": _CANDIDATE,
        "log_path": str(log_path),
    }
    run = SimpleNamespace(run_id="run-803", candidate_head=_CANDIDATE)
    return run, job, ledger_path


def test_verification_ledger_context_requires_exact_candidate_and_pytest(
    tmp_path: Path,
) -> None:
    run, job, ledger_path = _write_build_ledger(tmp_path)

    context = manager._verification_gate_ledger_context(run, job)

    assert context == {
        "candidate": _CANDIDATE,
        "path": str(ledger_path),
        "sha256": terminal_contract.gate_ledger_digest(
            json.loads(ledger_path.read_text(encoding="utf-8"))
        ),
        "gates": [
            {
                "name": "pytest",
                "command": "python3 -m pytest -q",
                "exit_code": 0,
                "status": "passed",
            }
        ],
        "result_summary": "pytest passed (exit 0)",
    }

    mismatch_run = SimpleNamespace(run_id="run-803", candidate_head="b" * 40)
    assert manager._verification_gate_ledger_context(mismatch_run, job) is None

    wrong_ledger_run, wrong_ledger_job, _ = _write_build_ledger(
        tmp_path / "wrong-ledger", ledger_head="b" * 40
    )
    assert manager._verification_gate_ledger_context(
        wrong_ledger_run, wrong_ledger_job
    ) is None
    assert manager._verification_gate_ledger_context(
        run, {**job, "workflow_test_policy": "red-required"}
    ) is None

    missing_run, missing_job, _ = _write_build_ledger(tmp_path / "missing")
    Path(terminal_contract.gate_ledger_path(missing_job["log_path"])).unlink()
    assert manager._verification_gate_ledger_context(missing_run, missing_job) is None


def test_verification_prompt_preserves_manager_full_suite_result(
    tmp_path: Path,
) -> None:
    run, job, _ = _write_build_ledger(tmp_path)
    context = manager._verification_gate_ledger_context(run, job)
    step = WorkflowStep(
        phase="verify",
        persona="reviewer",
        card="verification",
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=("reports/verify/work.md",),
    )

    prompt = manager._workflow_job_prompt(
        SimpleNamespace(
            run_id="run-803",
            work_id="work-803",
            repo="owner/repo",
            source_revision="rev",
            candidate_head=_CANDIDATE,
            openspec_refs=(),
        ),
        step,
        builder_job_id="build-job",
        coordinator_root=tmp_path,
        candidate_checkout="candidate",
        manager_gate_ledger=context,
        env={},
    )

    assert "manager_gate_ledger" in prompt
    assert str(context["sha256"]) in prompt
    assert "pytest passed (exit 0)" in prompt
    assert "ACL" in prompt
    assert "read-only" in prompt
    assert "TMPDIR" in prompt
    assert "must not override" in prompt
    assert "full-suite result" in prompt


def test_verification_prompt_keeps_failed_manager_gate_fail_closed(
    tmp_path: Path,
) -> None:
    run, job, _ = _write_build_ledger(tmp_path, exit_code=2)
    context = manager._verification_gate_ledger_context(run, job)
    step = WorkflowStep(
        phase="verify",
        persona="reviewer",
        card="verification",
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
    )

    prompt = manager._workflow_job_prompt(
        SimpleNamespace(
            run_id="run-803",
            work_id="work-803",
            repo="owner/repo",
            source_revision="rev",
            candidate_head=_CANDIDATE,
            openspec_refs=(),
        ),
        step,
        builder_job_id="build-job",
        coordinator_root=tmp_path,
        candidate_checkout="candidate",
        manager_gate_ledger=context,
        env={},
    )

    assert "pytest failed (exit 2)" in prompt
    assert "must not override" in prompt
    assert "focused green" in prompt
    assert "must not be reported verified" in prompt


def test_verification_prompt_without_candidate_ledger_is_unchanged(
    tmp_path: Path,
) -> None:
    step = WorkflowStep(
        phase="verify",
        persona="reviewer",
        card="verification",
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
    )
    run = SimpleNamespace(
        run_id="run-803",
        work_id="work-803",
        repo="owner/repo",
        source_revision="rev",
        candidate_head=_CANDIDATE,
        openspec_refs=(),
    )

    prompt = manager._workflow_job_prompt(
        run,
        step,
        builder_job_id="build-job",
        coordinator_root=tmp_path,
        candidate_checkout="candidate",
        env={},
    )

    assert "manager_gate_ledger" not in prompt
    assert "must not override" not in prompt


def test_verification_dispatch_attaches_candidate_bound_build_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    verify_step = WorkflowStep(
        phase="verify",
        persona="reviewer",
        card="verification",
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
    )
    run = registry._manager_create_workflow_run(
        work_id="work-803",
        repo="owner/repo",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="verification-ledger-test",
        current_phase="verify",
        steps=(verify_step,),
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={"verify": 1},
        candidate_head=_CANDIDATE,
        gate_status="running",
    )
    build_log = tmp_path / "build.jsonl"
    builder = registry.create_job(
        task="build-803",
        persona="builder",
        branch="feature/work-803",
        pane="",
        worktree=str(repo),
        executor="codex",
        model_id="builder",
        independence_domain="anthropic",
        subject_head=_CANDIDATE,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(repo),
        workflow_input_root=str(repo),
        source_revision=run.source_revision,
    )
    registry.attach_launch_handle(
        builder["job_id"],
        executor="codex",
        model_id="builder",
        session_name="build-803",
        pid=10,
        log_path=str(build_log),
    )
    registry.update_headless_result(builder["job_id"], status="exited", exit_code=0)
    persisted_builder = registry.get_job(builder["job_id"])
    ledger_path = terminal_contract.gate_ledger_path(
        manager._job_control_log_path(persisted_builder, str(persisted_builder["log_path"]))
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        gate_ledger.encode_ledger(
            gate_ledger.build_ledger(
                [
                    {
                        "name": "pytest",
                        "command": "python3 -m pytest -q",
                        "exit_code": 0,
                        "status": "passed",
                        "detail": "",
                    }
                ],
                slice_id=builder["job_id"],
                worktree_state={"head": _CANDIDATE, "probe": "ok"},
            )
        ),
        encoding="utf-8",
    )

    candidate_sandbox = tmp_path / "candidate-sandbox"
    candidate_sandbox.mkdir()
    monkeypatch.setattr(
        manager,
        "_executor_backoff_admission_report",
        lambda candidates, **_kwargs: {
            "eligible": tuple(candidates),
            "skipped": [],
            "unknown": [],
        },
    )
    monkeypatch.setattr(manager, "_workflow_input_snapshot", lambda **_kwargs: ())
    monkeypatch.setattr(manager, "_workflow_output_baseline", lambda *_args: ())
    monkeypatch.setattr(
        manager,
        "_reviewer_candidate_workspace",
        lambda **_kwargs: repo,
    )
    monkeypatch.setattr(
        manager,
        "_create_reviewer_sandbox",
        lambda **_kwargs: (candidate_sandbox, candidate_sandbox),
    )
    monkeypatch.setattr(manager, "_validate_workflow_input_snapshot", lambda *_args, **_kwargs: None)
    prompts: list[str] = []

    class Launcher:
        def as_review_only(self, *, terminal_kind):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            prompts.append(prompt)
            return LaunchHandle(
                executor="codex",
                model_id="reviewer",
                session_name=slice_id,
                pid=11,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "reviewer",
                "independence_domain": "openai",
                "capabilities": ["review"],
            }
        ]
    )
    job = manager.dispatch_workflow_card(
        SimpleNamespace(_registry=registry),
        run=run,
        identities=identities,
        launcher_factory=lambda _identity: Launcher(),
        coordinator_root=tmp_path / "coordinator",
    )

    assert job is not None
    assert len(prompts) == 1, f"dispatch result: {job!r}"
    contract = json.loads(prompts[0].split("Contract: ", 1)[1])
    assert contract["manager_gate_ledger"]["candidate"] == _CANDIDATE
    assert contract["manager_gate_ledger"]["sha256"] == terminal_contract.gate_ledger_digest(
        json.loads(ledger_path.read_text(encoding="utf-8"))
    )
    assert contract["manager_gate_ledger"]["result_summary"] == "pytest passed (exit 0)"
