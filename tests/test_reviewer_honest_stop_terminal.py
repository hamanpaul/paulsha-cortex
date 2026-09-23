"""RED regression coverage for issue #874's verify/review explicit-stop gap."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, manager_daemon
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowStep
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo


VERIFY_SUMMARY = (
    "Cannot reproduce official preflight test gate due to missing operator runtime "
    "in the reviewer sandbox."
)
VERIFY_HOST_PREFLIGHT_STATUS = (
    "Passed 871 tests and 34 subtests as reported by host adjudication."
)
VERIFY_SANDBOX_LIMITATIONS = (
    "The complete operator runtime (.venv) and 'paulsha_cortex' module are missing "
    "in the reviewer sandbox, causing scripts/preflight-tests.sh … to fail."
)
VERIFY_FAILED_SUMMARY = (
    "Candidate only implements accepted spec R5 and leaves R1-R4/R6-R8 unmet."
)
VERIFY_FAILED_DETAILS = (
    "`--footer` rejects the spec's own acceptance example, and the acceptance "
    "pytest command cannot collect."
)
REVIEW_FAILED_REASON = (
    "Review found a real R3 bug: codex enabled=false rows are still collected and rendered."
)
REVIEW_BLOCKING_FINDING = {
    "category": "blocking-findings",
    "severity": "high",
    "summary": "codex enabled=false rows are still collected and rendered.",
    "details": "The reviewer reported a real accepted-scope regression in the rendered roster.",
}


def _manifest():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(
        combo,
        cards,
        "reviewer honest stop terminal",
        change="reviewer-honest-stop-terminal",
    )
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def _workflow_steps(current_phase: str) -> tuple[WorkflowStep, ...]:
    passed = {"claim", "define", "plan", "build"}
    if current_phase == "review":
        passed.add("verify")
    return tuple(
        WorkflowStep.from_dict(
            {
                **step.to_dict(),
                "gate_result": "passed" if step.phase in passed else "pending",
            }
        )
        for step in _manifest().steps
    )


def _planning_authority(repo_root: Path) -> tuple[tuple[PlanningArtifactAuthority, ...], dict[str, str]]:
    sources = {
        "docs/superpowers/plans/reviewer-honest-stop-terminal.md": (
            b"# Accepted plan\n\nReview against this.\n"
        ),
        "docs/superpowers/specs/reviewer-honest-stop-terminal-spec.md": (
            b"# Accepted spec\n\nHonor this authority too.\n"
        ),
    }
    authority_hashes: dict[str, str] = {}
    authorities: list[PlanningArtifactAuthority] = []
    for ref, body in sources.items():
        target = repo_root / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        authority_hashes[ref] = digest
        authorities.append(
            PlanningArtifactAuthority(
                ref=ref,
                kind="plan" if "/plans/" in ref else "spec",
                work_id="reviewer-honest-stop-terminal",
                baseline_sha256=digest,
            )
        )
    return tuple(authorities), authority_hashes


def _reviewer_terminal_fixture(
    tmp_path: Path,
    *,
    current_phase: str,
    executor: str,
    model_id: str,
    independence_domain: str,
):
    repo_root = tmp_path / "candidate"
    repo_root.mkdir(parents=True)
    authorities, authority_hashes = _planning_authority(repo_root)
    candidate = "a" * 40
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=coordinator_root / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id="reviewer-honest-stop-terminal",
        repo="owner/repo",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo_root),
        combo="feature-oneshot",
        current_phase=current_phase,
        steps=_workflow_steps(current_phase),
        issue_refs=("hamanpaul/paulsha-cortex#874",),
        openspec_refs=(),
        pr_refs=(),
        attempts={current_phase: 1},
        candidate_head=candidate,
        verified_head=candidate if current_phase == "review" else None,
        gate_status="running",
        planning_authority=authorities,
    )
    builder_job = registry.create_job(
        task="builder",
        persona="builder",
        kind="build",
        branch="feature/work",
        pane="",
        worktree=str(repo_root),
        executor="codex",
        model_id="builder",
        independence_domain="openai",
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        workflow_repo_root=str(repo_root),
        source_revision=run.source_revision,
    )
    registry.update_headless_result(builder_job["job_id"], status="exited", exit_code=0)

    card = "verification" if current_phase == "verify" else "code-review"
    step = next(item for item in _manifest().steps if item.card == card)
    workflow_inputs = ()
    workflow_input_snapshot = ()
    if current_phase == "review":
        workflow_inputs = manager._reviewer_input_patterns(
            run,
            manager._effective_workflow_inputs(run, step),
        )
        workflow_input_snapshot = manager._workflow_input_snapshot(
            run=run,
            repo_root=repo_root,
            patterns=workflow_inputs,
            coordinator_root=coordinator_root,
        )
    review_job = registry.create_job(
        task=card,
        persona="reviewer",
        kind="review",
        branch="feature/work",
        pane="",
        worktree=str(repo_root),
        executor=executor,
        model_id=model_id,
        independence_domain=independence_domain,
        subject_head=candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=card,
        workflow_phase=current_phase,
        workflow_repo_root=str(repo_root),
        workflow_input_root=str(repo_root),
        workflow_inputs=workflow_inputs,
        workflow_input_snapshot=workflow_input_snapshot,
        workflow_outputs=tuple(step.outputs),
        workflow_output_baseline=(),
        workflow_builder_job_id=builder_job["job_id"],
        source_revision=run.source_revision,
    )
    return registry, run, review_job, authority_hashes, coordinator_root


def _verification_payload(
    *,
    status: str,
    summary: str,
    details: object,
    reports: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": status,
        "summary": summary,
        "details": details,
        "reports": list(reports or []),
    }


def _review_payload(
    *,
    status: str = "failed",
    authority_hashes: dict[str, str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": "workflow-review-result",
        "status": status,
        "reason": REVIEW_FAILED_REASON,
        "findings": [dict(REVIEW_BLOCKING_FINDING)],
        "reports": [],
    }
    if authority_hashes is not None:
        payload["authority_hashes"] = dict(authority_hashes)
    return payload


def _write_claude_terminal_log(job: dict[str, object], payload: dict[str, object]) -> Path:
    log_path = Path(job["worktree"]) / f"{job['job_id']}.jsonl"
    log_path.write_text(
        json.dumps(
            {
                "type": "result",
                "result": json.dumps(payload, ensure_ascii=False),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return log_path


def _agy_response(payload: dict[str, object]) -> str:
    return (
        "```json\n"
        "{\n  \"schema_version\": 1,\n  \"kind\": \"workflow-verification-result\"\n}\n"
        "```\n"
        + json.dumps(
            {
                **payload,
                "toolAction": "Finishing the interaction",
                "toolSummary": "Workflow verification result",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _write_agy_terminal_log(job: dict[str, object], payload: dict[str, object]) -> Path:
    log_path = Path(job["worktree"]) / f"{job['job_id']}.jsonl"
    log_path.write_text(
        json.dumps({"response": _agy_response(payload)}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return log_path


def _attach_terminal_log(
    registry: JobRegistry,
    job: dict[str, object],
    *,
    log_path: Path,
) -> None:
    registry.attach_launch_handle(
        job["job_id"],
        executor=str(job["executor"]),
        model_id=str(job["model_id"]),
        log_path=str(log_path),
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)


class _ResumeDispatcher:
    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry

    def poll_headless_done(self, job_id: str) -> dict:
        return self._registry.get_job(job_id)


def _resume(
    registry: JobRegistry,
    run_id: str,
    *,
    coordinator_root: Path,
    operator_resume: bool = False,
) -> dict[str, object]:
    return manager.resume_workflow_run(
        _ResumeDispatcher(registry),
        run_id=run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda identity: identity,
        coordinator_root=coordinator_root,
        operator_resume=operator_resume,
    )


def _explicit_stop_gate_terminal(job: dict[str, object]) -> dict[str, object] | None:
    predicate = getattr(manager, "_explicit_stop_gate_terminal", None)
    assert callable(predicate), "manager._explicit_stop_gate_terminal is missing"
    return predicate(job)


@pytest.mark.parametrize(
    ("executor", "model_id", "independence_domain", "writer"),
    (
        ("agy", "gemini-3.1-pro-high", "google", _write_agy_terminal_log),
        ("claude", "claude-opus-5", "anthropic", _write_claude_terminal_log),
    ),
)
def test_verify_needs_human_terminal_becomes_explicit_stop_attention(
    tmp_path: Path,
    executor: str,
    model_id: str,
    independence_domain: str,
    writer,
) -> None:
    registry, run, verify_job, _authority_hashes, coordinator_root = _reviewer_terminal_fixture(
        tmp_path,
        current_phase="verify",
        executor=executor,
        model_id=model_id,
        independence_domain=independence_domain,
    )
    payload = _verification_payload(
        status="needs_human",
        summary=VERIFY_SUMMARY,
        details={
            "host_preflight_status": VERIFY_HOST_PREFLIGHT_STATUS,
            "sandbox_limitations": VERIFY_SANDBOX_LIMITATIONS,
        },
    )
    log_path = writer(verify_job, payload)
    _attach_terminal_log(registry, verify_job, log_path=log_path)

    result = _resume(registry, run.run_id, coordinator_root=coordinator_root)

    assert result["reason"] == "verification-terminal-explicit-stop"
    assert result["declared_status"] == "needs_human"
    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" in persisted.facets
    reason = dict(persisted.needs_human_reason)
    assert reason["reason"] == "verification-terminal-explicit-stop"
    assert VERIFY_SUMMARY in reason["detail"]
    assert VERIFY_HOST_PREFLIGHT_STATUS in reason["detail"]
    assert VERIFY_SANDBOX_LIMITATIONS in reason["detail"]
    assert reason["evidence_refs"] == [str(log_path)]
    diagnostics = result["terminal_diagnostics"]
    assert diagnostics["reason"] == (
        "workflow verification terminal reported non-passing status: needs_human"
    )
    assert diagnostics["authority_granted"] is False
    assert diagnostics["model_diagnostics"]["summary"] == VERIFY_SUMMARY
    assert (
        diagnostics["model_diagnostics"]["details.host_preflight_status"]
        == VERIFY_HOST_PREFLIGHT_STATUS
    )
    assert (
        diagnostics["model_diagnostics"]["details.sandbox_limitations"]
        == VERIFY_SANDBOX_LIMITATIONS
    )


def test_verify_failed_string_details_and_empty_reports_become_explicit_stop(
    tmp_path: Path,
) -> None:
    registry, run, verify_job, _authority_hashes, coordinator_root = _reviewer_terminal_fixture(
        tmp_path,
        current_phase="verify",
        executor="claude",
        model_id="claude-opus-5",
        independence_domain="anthropic",
    )
    payload = _verification_payload(
        status="failed",
        summary=VERIFY_FAILED_SUMMARY,
        details=VERIFY_FAILED_DETAILS,
    )
    log_path = _write_claude_terminal_log(verify_job, payload)
    _attach_terminal_log(registry, verify_job, log_path=log_path)

    result = _resume(registry, run.run_id, coordinator_root=coordinator_root)

    assert result["reason"] == "verification-terminal-explicit-stop"
    assert result["declared_status"] == "failed"
    assert result["terminal_diagnostics"]["reason"] == (
        "workflow verification terminal reported non-passing status: failed"
    )
    assert result["terminal_diagnostics"]["model_diagnostics"]["summary"] == VERIFY_FAILED_SUMMARY
    assert result["terminal_diagnostics"]["model_diagnostics"]["details"] == VERIFY_FAILED_DETAILS
    reason = dict(registry.get_workflow_run(run.run_id).needs_human_reason)
    assert reason["reason"] == "verification-terminal-explicit-stop"
    assert VERIFY_FAILED_SUMMARY in reason["detail"]
    assert VERIFY_FAILED_DETAILS in reason["detail"]
    assert reason["evidence_refs"] == [str(log_path)]


@pytest.mark.parametrize("include_authority_hashes", (False, True))
def test_review_failed_terminal_becomes_explicit_stop(
    tmp_path: Path,
    include_authority_hashes: bool,
) -> None:
    registry, run, review_job, authority_hashes, coordinator_root = _reviewer_terminal_fixture(
        tmp_path,
        current_phase="review",
        executor="claude",
        model_id="claude-opus-5",
        independence_domain="anthropic",
    )
    payload = _review_payload(
        authority_hashes=authority_hashes if include_authority_hashes else None,
    )
    log_path = _write_claude_terminal_log(review_job, payload)
    _attach_terminal_log(registry, review_job, log_path=log_path)

    result = _resume(registry, run.run_id, coordinator_root=coordinator_root)

    assert result["reason"] == "review-terminal-explicit-stop"
    assert result["declared_status"] == "failed"
    diagnostics = result["terminal_diagnostics"]
    assert diagnostics["reason"] == "workflow review terminal reported non-passing status: failed"
    assert diagnostics["model_diagnostics"]["reason"] == REVIEW_FAILED_REASON
    assert "findings[0]" in diagnostics["model_diagnostics"]
    assert REVIEW_BLOCKING_FINDING["summary"] in diagnostics["model_diagnostics"]["findings[0]"]
    assert diagnostics["authority_granted"] is False
    reason = dict(registry.get_workflow_run(run.run_id).needs_human_reason)
    assert reason["reason"] == "review-terminal-explicit-stop"
    assert REVIEW_FAILED_REASON in reason["detail"]
    assert REVIEW_BLOCKING_FINDING["summary"] in reason["detail"]
    assert reason["evidence_refs"] == [str(log_path)]


def test_explicit_stop_preserves_state_and_operator_resume_is_idempotent(
    tmp_path: Path,
) -> None:
    registry, run, review_job, authority_hashes, coordinator_root = _reviewer_terminal_fixture(
        tmp_path,
        current_phase="review",
        executor="claude",
        model_id="claude-opus-5",
        independence_domain="anthropic",
    )
    payload = _review_payload(authority_hashes=authority_hashes)
    log_path = _write_claude_terminal_log(review_job, payload)
    _attach_terminal_log(registry, review_job, log_path=log_path)
    initial_steps = tuple(step.gate_result for step in run.steps)
    initial_attempts = dict(run.attempts)
    initial_job_ids = [job["job_id"] for job in registry.list_jobs()]

    first = _resume(registry, run.run_id, coordinator_root=coordinator_root)
    second = _resume(
        registry,
        run.run_id,
        coordinator_root=coordinator_root,
        operator_resume=True,
    )

    assert first["reason"] == "review-terminal-explicit-stop"
    assert second["reason"] == "review-terminal-explicit-stop"
    assert second["declared_status"] == "failed"
    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.attempts == initial_attempts
    assert persisted.candidate_head == run.candidate_head
    assert persisted.verified_head == run.verified_head
    assert tuple(step.gate_result for step in persisted.steps) == initial_steps
    assert [job["job_id"] for job in registry.list_jobs()] == initial_job_ids
    assert all(job.get("workflow_evidence") is None for job in registry.list_jobs())
    entry = manager.workflow_status_entry(registry, persisted)
    assert {"abandon", "retry-card"}.issubset(set(entry["next_actions"]))
    assert dict(persisted.needs_human_reason)["evidence_refs"] == [str(log_path)]


def test_periodic_runner_preserves_explicit_stop_reason(tmp_path: Path) -> None:
    registry, run, review_job, authority_hashes, _coordinator_root = _reviewer_terminal_fixture(
        tmp_path,
        current_phase="review",
        executor="claude",
        model_id="claude-opus-5",
        independence_domain="anthropic",
    )
    log_path = _write_claude_terminal_log(
        review_job,
        _review_payload(authority_hashes=authority_hashes),
    )
    _attach_terminal_log(registry, review_job, log_path=log_path)

    periodic = manager_daemon.build_periodic_tick_runner(
        dispatcher=_ResumeDispatcher(registry),
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        run_tick_fn=lambda *args, **kwargs: {
            "dispatch_skipped": False,
            "dispatched": [],
            "completed": [],
            "errors": [],
            "reaped": None,
        },
        scan_specs_fn=lambda _specs_dir: [],
        auto_claim_fn=lambda: [],
        workflow_identity_registry=IdentityRegistry.from_rows([]),
    )
    periodic()

    reason = dict(registry.get_workflow_run(run.run_id).needs_human_reason)
    assert reason["reason"] == "review-terminal-explicit-stop"
    assert reason["reason"] != "resume-workflow-failed"
    assert reason["evidence_refs"] == [str(log_path)]


def test_explicit_stop_discards_reviewer_sandbox_with_candidate_drift_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, run, review_job, authority_hashes, coordinator_root = _reviewer_terminal_fixture(
        tmp_path,
        current_phase="review",
        executor="claude",
        model_id="claude-opus-5",
        independence_domain="anthropic",
    )
    _attach_terminal_log(
        registry,
        review_job,
        log_path=_write_claude_terminal_log(
            review_job,
            _review_payload(authority_hashes=authority_hashes),
        ),
    )
    calls: list[dict[str, object]] = []

    def fake_discard(job, *, coordinator_root, require_candidate_unchanged):
        calls.append(
            {
                "job_id": job["job_id"],
                "coordinator_root": str(coordinator_root),
                "require_candidate_unchanged": require_candidate_unchanged,
            }
        )

    monkeypatch.setattr(manager, "_discard_reviewer_sandbox", fake_discard)

    result = _resume(registry, run.run_id, coordinator_root=coordinator_root)

    assert result["reason"] == "review-terminal-explicit-stop"
    assert calls == [
        {
            "job_id": review_job["job_id"],
            "coordinator_root": str(coordinator_root),
            "require_candidate_unchanged": True,
        }
    ]


def test_reviewer_candidate_drift_becomes_needs_human_instead_of_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, run, review_job, authority_hashes, coordinator_root = _reviewer_terminal_fixture(
        tmp_path,
        current_phase="review",
        executor="claude",
        model_id="claude-opus-5",
        independence_domain="anthropic",
    )
    _attach_terminal_log(
        registry,
        review_job,
        log_path=_write_claude_terminal_log(
            review_job,
            _review_payload(authority_hashes=authority_hashes),
        ),
    )

    def drift(*args, **kwargs):
        raise ValueError("workflow reviewer modified Candidate checkout")

    monkeypatch.setattr(manager, "_discard_reviewer_sandbox", drift)

    result = _resume(registry, run.run_id, coordinator_root=coordinator_root)

    assert result["reason"] == "reviewer-candidate-drift"
    persisted = registry.get_workflow_run(run.run_id)
    reason = dict(persisted.needs_human_reason)
    assert reason["reason"] == "reviewer-candidate-drift"
    assert "status=failed" in reason["detail"]
    assert "workflow reviewer modified Candidate checkout" in reason["detail"]


@pytest.mark.parametrize(
    ("phase", "executor", "model_id", "independence_domain", "writer", "payload_factory"),
    (
        (
            "verify",
            "agy",
            "gemini-3.1-pro-high",
            "google",
            _write_agy_terminal_log,
            lambda authority_hashes: _verification_payload(
                status="needs_human",
                summary=VERIFY_SUMMARY,
                details={
                    "host_preflight_status": VERIFY_HOST_PREFLIGHT_STATUS,
                    "sandbox_limitations": VERIFY_SANDBOX_LIMITATIONS,
                },
            ),
        ),
        (
            "review",
            "claude",
            "claude-opus-5",
            "anthropic",
            _write_claude_terminal_log,
            lambda authority_hashes: _review_payload(authority_hashes=authority_hashes),
        ),
    ),
)
def test_explicit_stop_predicate_accepts_verify_and_review_nonpassing_shapes(
    tmp_path: Path,
    phase: str,
    executor: str,
    model_id: str,
    independence_domain: str,
    writer,
    payload_factory,
) -> None:
    registry, _run, review_job, authority_hashes, _coordinator_root = _reviewer_terminal_fixture(
        tmp_path,
        current_phase=phase,
        executor=executor,
        model_id=model_id,
        independence_domain=independence_domain,
    )
    payload = payload_factory(authority_hashes)
    _attach_terminal_log(
        registry,
        review_job,
        log_path=writer(review_job, payload),
    )

    assert _explicit_stop_gate_terminal(registry.get_job(review_job["job_id"])) == payload


@pytest.mark.parametrize(
    ("phase", "payload"),
    (
        (
            "verify",
            _verification_payload(
                status="needs_human",
                summary=" ",
                details={"host_preflight_status": VERIFY_HOST_PREFLIGHT_STATUS},
            ),
        ),
        (
            "verify",
            {
                **_verification_payload(
                    status="needs_human",
                    summary=VERIFY_SUMMARY,
                    details={"host_preflight_status": VERIFY_HOST_PREFLIGHT_STATUS},
                ),
                "extra": 1,
            },
        ),
        (
            "verify",
            _verification_payload(
                status="needs_human",
                summary=VERIFY_SUMMARY,
                details={"host_preflight_status": VERIFY_HOST_PREFLIGHT_STATUS},
                reports=[{"path": "reports/verify/x.md"}],
            ),
        ),
        (
            "verify",
            {
                **_verification_payload(
                    status="needs_human",
                    summary=VERIFY_SUMMARY,
                    details={"host_preflight_status": VERIFY_HOST_PREFLIGHT_STATUS},
                ),
                "schema_version": True,
            },
        ),
        (
            "verify",
            {
                **_verification_payload(
                    status="needs_human",
                    summary=VERIFY_SUMMARY,
                    details={"host_preflight_status": VERIFY_HOST_PREFLIGHT_STATUS},
                ),
                "kind": "workflow-review-result",
            },
        ),
        (
            "review",
            {
                **_review_payload(),
                "extra": 1,
            },
        ),
        (
            "review",
            {
                **_review_payload(),
                "findings": [42],
            },
        ),
        (
            "review",
            {
                **_review_payload(),
                "reports": [42],
            },
        ),
        (
            "review",
            {
                **_review_payload(),
                "reports": [{"path": "reports/review/x.md"}],
            },
        ),
    ),
)
def test_counterexample_terminals_stay_fail_closed(
    tmp_path: Path,
    phase: str,
    payload: dict[str, object],
) -> None:
    registry, run, review_job, _authority_hashes, coordinator_root = _reviewer_terminal_fixture(
        tmp_path,
        current_phase=phase,
        executor="claude",
        model_id="claude-opus-5",
        independence_domain="anthropic",
    )
    _attach_terminal_log(
        registry,
        review_job,
        log_path=_write_claude_terminal_log(review_job, payload),
    )
    job = registry.get_job(review_job["job_id"])

    assert _explicit_stop_gate_terminal(job) is None
    assert manager._retryable_nonpassing_workflow_terminal(job) is False
    with pytest.raises(ValueError):
        _resume(registry, run.run_id, coordinator_root=coordinator_root)


def test_plan_and_build_jobs_do_not_match_reviewer_explicit_stop_predicate(
    tmp_path: Path,
) -> None:
    log = tmp_path / "terminal.jsonl"
    payload = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "needs_human",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    for phase in ("plan", "build"):
        job = {
            "job_id": f"job-{phase}",
            "workflow_evidence": None,
            "status": "exited",
            "exit_code": 0,
            "workflow_phase": phase,
            "workflow_run_id": "run",
            "workflow_card": "card",
            "log_path": str(log),
        }
        assert _explicit_stop_gate_terminal(job) is None


def test_verify_and_review_terminals_do_not_gain_plan_build_retry_authority(
    tmp_path: Path,
) -> None:
    registry, _run, verify_job, _authority_hashes, _coordinator_root = _reviewer_terminal_fixture(
        tmp_path,
        current_phase="verify",
        executor="agy",
        model_id="gemini-3.1-pro-high",
        independence_domain="google",
    )
    verify_payload = _verification_payload(
        status="needs_human",
        summary=VERIFY_SUMMARY,
        details={
            "host_preflight_status": VERIFY_HOST_PREFLIGHT_STATUS,
            "sandbox_limitations": VERIFY_SANDBOX_LIMITATIONS,
        },
    )
    _attach_terminal_log(
        registry,
        verify_job,
        log_path=_write_agy_terminal_log(verify_job, verify_payload),
    )
    assert manager._retryable_nonpassing_workflow_terminal(
        registry.get_job(verify_job["job_id"])
    ) is False

    registry2, _run2, review_job, authority_hashes, _coordinator_root2 = _reviewer_terminal_fixture(
        tmp_path / "review",
        current_phase="review",
        executor="claude",
        model_id="claude-opus-5",
        independence_domain="anthropic",
    )
    _attach_terminal_log(
        registry2,
        review_job,
        log_path=_write_claude_terminal_log(
            review_job,
            _review_payload(authority_hashes=authority_hashes),
        ),
    )
    assert manager._retryable_nonpassing_workflow_terminal(
        registry2.get_job(review_job["job_id"])
    ) is False
